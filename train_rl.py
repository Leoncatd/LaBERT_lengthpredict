import argparse
import logging
import os
import time
import json

import torch
import torch.nn as nn
import numpy as np
import torch.nn.functional as F
from torch.nn.parallel import DistributedDataParallel
from torch.nn.utils import clip_grad_norm_
from transformers.modeling_bert import BertConfig
from transformers.optimization import AdamW, WarmupCosineSchedule

from config import _C as config
from dataset import COCOCaptionDataset, collate_fn_train
from modeling import Generator, LabelSmoothingLoss
from utils import get_rank, mkdir, synchronize
from utils.checkpointer import Checkpointer
from utils.dataloader import make_data_loader
from utils.logger import setup_logger
from utils.tokenizer import EOS, MASK, PAD, num_tokens, LENGTH, tokenizer

from pycocoevalcap.cider.cider import Cider
from pycocoevalcap.meteor.meteor import Meteor
from pycocoevalcap.tokenizer.ptbtokenizer import PTBTokenizer


class SelfCriticalLoss(torch.nn.Module):
    def __init__(self, scorers, ptb_tokenizer, gt_caption, entropy_weight):
        super(SelfCriticalLoss, self).__init__()
        self.scorers = scorers
        self.tokenizer = ptb_tokenizer
        self.gt_captions = gt_caption
        self.entropy_weight = entropy_weight

    def forward(self, new_caption, probs, image_id):
        """
        Args:
            old_caption: (N, L), long
            new_caption: (N, L), long
            probs: (N,), float
            image_id: (N,), long
            mask: (N, L), float
        """
        # new_caption = new_caption.cpu().numpy()
        # image_id = image_id.cpu().numpy()

        ref = dict()
        new_hypo = dict()
        for new, id in zip(new_caption, image_id):
            id = str(id.cpu().numpy())
            if id in ref.keys():
                id_ = id + '_'
            else:
                id_ = id
            ref[id_] = self.gt_captions[id]
            new = tokenizer.decode(new.cpu().numpy(), end_flags=[EOS])
            new_hypo[id_] = [{'caption': new}]
        new_hypo = self.tokenizer.tokenize(new_hypo)

        rewards = 0.0
        for (scorer, weight) in self.scorers:
            _, new_scores = scorer.compute_score(ref, new_hypo)
            rewards += np.asarray(new_scores) * weight
        rewards = torch.from_numpy(rewards).to(probs.device).float().unsqueeze(1)
        logprobs = probs.log()
        entropy = (-logprobs * probs).mean()
        ascent_objective = (logprobs * rewards).mean()
        ascent_objective += self.entropy_weight * entropy

        return -ascent_objective, rewards.mean()

def train(generator, optimizer, data_loader, scheduler, checkpointer,
          device, log_time, checkpoint_time, arguments):
    logger = logging.getLogger("train")
    logger.info("Start training")
    max_iter = len(data_loader)
    start_iter = arguments['iteration']
    generator.train()

    if config.loss.balance_weight != 1.0:
        balance_weight = torch.ones(
            num_tokens, dtype=torch.float32, device=device)
        balance_weight[EOS] = config.loss.balance_weight
    else:
        balance_weight = None

    criterion = LabelSmoothingLoss(
        num_tokens, balance_weight, config.loss.label_smoothing)
    crossEntropyLoss = nn.CrossEntropyLoss()

    scorers = [(Cider(), 0.1), (Meteor(), 1.0)]
    ptb_tokenizer = PTBTokenizer()
    with open(os.path.join(config.data_dir, 'id2captions_train.json')) as f:
        gt_captions = json.load(f)
    gt_captions = ptb_tokenizer.tokenize(gt_captions)
    rl_criterion = SelfCriticalLoss(
        scorers, ptb_tokenizer, gt_captions, config.loss.entropy_weight)



    end = time.time()
    for iteration, batch in enumerate(data_loader, start_iter):
        iteration = iteration + 1
        arguments['iteration'] = iteration

        token_type_ids = batch[0].to(device)  # (N, L), long
        input_token_ids = batch[1].to(device)  # (N, L), long
        masked_token_ids = batch[2].to(device)  # (N, L), long
        region_features = batch[3].to(device)  # (N, 100, 2048), float
        region_class = batch[4].to(device)  # (N, 100, 1601), float
        region_spatial = batch[5].to(device)  # (N, 100, 6), float
        gt_maxlength = batch[6].to(device) # (N), float
        image_id = batch[7].to(device)

        num_img_tokens = region_spatial.size(1)
        seq_length = input_token_ids.size(1)
        batch_size = input_token_ids.size(0)
        pred_levelp_list = list()
        pred_ids_list = list()



        region_spatial[:, :, [0, 2]] /= region_spatial[:, :, [2]] + 1e-5
        region_spatial[:, :, [1, 3]] /= region_spatial[:, :, [3]] + 1e-5
        rel_area = (region_spatial[:, :, [3]] - region_spatial[:, :, [1]]) * \
                   (region_spatial[:, :, [2]] - region_spatial[:, :, [0]])
        region_spatial = torch.cat((region_spatial[:, :, :4],
            rel_area.clamp_(0), region_spatial[:, :, 5:]), dim=-1)
        position_features = torch.cat((F.layer_norm(region_spatial, [6]),
            F.layer_norm(region_class, [1601])), dim=-1)

        position_ids = torch.arange(seq_length, dtype=torch.long, device=device)
        position_ids = position_ids.unsqueeze(0).expand_as(input_token_ids)

        region_type = position_ids.new_full(
            region_features.shape[:2], len(config.boundaries) + 1)
        '''
        token_type_ids = torch.cat((region_type, token_type_ids), dim=1)
        
        attention_mask = (masked_token_ids != PAD).float()
        _attention_mask = attention_mask.new_ones((batch_size, num_img_tokens))
        attention_mask = torch.cat((_attention_mask, attention_mask), dim=1)

        mask_position = (masked_token_ids == MASK).to(torch.long).view(-1)
        mask_position = mask_position.nonzero().squeeze()
        '''
        for l, (low, high) in enumerate(config.boundaries, 1):
            token_type_ids = region_class.new_full((batch_size, high), l, dtype=torch.long)
            masked_token_ids = token_type_ids.new_full((batch_size, high), MASK)
            attention_mask = rel_area.new_ones((batch_size, high + num_img_tokens))
            position_ids = torch.arange(high, dtype=torch.long, device=device)
            position_ids = position_ids.unsqueeze(0).expand_as(masked_token_ids)
            token_type_ids = torch.cat((region_type, token_type_ids), dim=1)

            pred_scores, length_score = generator(
                region_features, position_features,
                masked_token_ids, token_type_ids,
                position_ids, attention_mask)

            pred_scores = pred_scores[:, num_img_tokens:, :]
            #pred_scores = pred_scores.contiguous().view(-1, num_tokens)
            #pred_scores = pred_scores[mask_position]
            #gt_length = token_type_ids[:, 100]
            #gt_token_ids = input_token_ids[:,1:]#.contiguous().view(-1)[mask_position]
            _, pred_token_ids = F.softmax(pred_scores, dim=-1).max(dim=-1)

            #pred_token_ids.
            pred_zeros = torch.zeros(pred_token_ids.shape[0], 25).to(pred_token_ids.device)
            pred_zeros[:, :pred_token_ids.shape[1]] = pred_token_ids


            pred_levelp_list.append(length_score)
            pred_ids_list.append(pred_zeros)

        pred_levelp_list = torch.stack(pred_levelp_list, 1)

        levelp_max_fin, levelp_max = torch.max(pred_levelp_list,1) #b*1
        pred_ids_fin = [pred_ids_list[levelp_max[i,0]][i] for i in range(len(levelp_max))]
        #pred_ids_fin = [pred_ids_list[levelp_max[0, 0]][0] for i in range(len(levelp_max))]



        masker_loss, masker_reward = rl_criterion(pred_ids_fin, levelp_max_fin, image_id)

        #loss_length = crossEntropyLoss(length_score, gt_maxlength-1)

        #loss = loss_words + masker_loss
        loss = masker_loss


        optimizer.zero_grad()
        loss.backward()
        clip_grad_norm_(generator.parameters(), config.solver.grad_clip)
        optimizer.step()
        scheduler.step()
        batch_time = time.time() - end
        end = time.time()

        if iteration % log_time == 0 or iteration == max_iter:
            logger.info(
                '  '.join([
                    "iter: {iter}", "time: {time:.4f}", "mem: {mem:.2f}",
                    "lr: {lr:.8f}","loss: {loss:.4f}"
                ]).format(
                    iter=iteration, time=batch_time, loss=loss,
                    #loss_words = loss_words, #loss_length = loss_length,
                    lr=optimizer.param_groups[0]["lr"],
                    mem=torch.cuda.max_memory_allocated() / 1024.0 ** 3,
                ))
        if iteration % checkpoint_time == 0 or iteration == max_iter:
            checkpointer.save("model_{:07d}".format(iteration), **arguments)


if __name__ == "__main__":
    os.environ["PATH"] += ':/home/dingning/anaconda3/envs/vlp/bin'
    parser = argparse.ArgumentParser(description="train")
    parser.add_argument("--local_rank", type=int, default=0)
    parser.add_argument("opts", default=None, nargs=argparse.REMAINDER)
    args = parser.parse_args()

    if config.distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group("nccl", init_method="env://")
        synchronize()

    config.merge_from_list(args.opts)
    config.freeze()

    save_dir = os.path.join(config.save_dir, f'train')
    mkdir(save_dir)
    logger = setup_logger("train", save_dir, get_rank())
    logger.info("Running with config:\n{}".format(config))

    arguments = {'iteration': 0}
    device = torch.device(config.device)

    bert_config = BertConfig(type_vocab_size=len(config.boundaries) + 2)
    generator = Generator(bert_config)
    generator = generator.to(device)

    optimizer = AdamW(
        params=generator.parameters(),
        lr=config.solver.lr,
        weight_decay=config.solver.weight_decay,
        betas=config.solver.betas
    )

    scheduler = WarmupCosineSchedule(
        optimizer=optimizer,
        warmup_steps=config.scheduler.warmup_steps,
        t_total=config.scheduler.max_steps
    )

    checkpointer = Checkpointer(
        model=generator,
        optimizer=optimizer,
        scheduler=scheduler,
        save_dir=save_dir,
        save_to_disk=get_rank() == 0,
        logger=logger
    )

    if config.model_path == '':
        generator.load_weights(config.pretrained_bert)
    else:
        extra_checkpoint_data = checkpointer.load(config.model_path)
        arguments.update(extra_checkpoint_data)

    dataset = COCOCaptionDataset(
        root=config.data_dir,
        split='trainrestval',
        boundaries=config.boundaries,
    )

    data_loader = make_data_loader(
        dataset=dataset,
        collate_fn=collate_fn_train,
        batch_size=config.samples_per_gpu,
        num_workers=config.num_workers,
        max_iter=config.scheduler.max_steps,
        split='trainrestval',
        is_distributed=config.distributed,
        start_iter=arguments['iteration'],
    )

    if config.distributed:
        generator = DistributedDataParallel(
            module=generator,
            device_ids=[args.local_rank],
            output_device=args.local_rank,
        )

    train(generator=generator,
          optimizer=optimizer,
          data_loader=data_loader,
          scheduler=scheduler,
          checkpointer=checkpointer,
          device=device,
          log_time=config.log_time,
          checkpoint_time=config.checkpoint_time,
          arguments=arguments)
