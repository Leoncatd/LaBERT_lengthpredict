import json


#inference的句子长度分布
f = json.load(open("/home/dingning/workspace/LaBERT_lengthpredict/output_4_lengthcls_mask_averlength/inference/model_0097500/caption_results_level.json"))


print(len(f))
print(f["391895"]['level'])
print(len(f.keys()))
dict = dict.fromkeys(['1', '2','3','4'], 0)
dict_a = {}


for key in f.keys():
    level = f[str(key)]['level']
    if level not in dict_a:
        dict_a[level] = 0
    dict_a[level]+=1


print(dict_a)


'''
f = json.load(open("/home/dingning/workspace/NAIC/data/id2captions_train.json"))
print(len(f))

length = {}
for key in f.keys():
    length_all = 0
    for i in  range(len(f[key])):
        length_all += len(f[key][i]['caption'].split())
    #key = f'{int(key):06d}'
    length[f'{int(key):06d}'] = round(length_all / len(f[key]))


path = "/home/dingning/workspace/LaBERT_lengthpredict/id2length_train_aver.json"
json_str = json.dumps(length, ensure_ascii=False, indent=4)  # 缩进4字符
with open(path, 'w') as json_file:
    json_file.write(json_str)


length_file = json.load(open("/home/dingning/workspace/LaBERT_lengthpredict/id2length_train_aver.json"))
print(len(length_file)) #118287
#print(length_file['522418']) #10.4
#print(length_file['184613']) #11.8
#print(length_file['318219']) #9.8
'''

'''
f = json.load(open("/home/dingning/workspace/NAIC/data/id2captions_train.json"))
print(len(f))


length = {}
for key in f.keys():
    max_length = 0
    for i in  range(len(f[key])):
        max_length = max(max_length,len(f[key][i]['caption'].split()))
    length[f'{int(key):06d}'] = max_length




path = "/home/dingning/workspace/LaBERT_lengthpredict/id2length_train_max.json"
json_str = json.dumps(length, ensure_ascii=False, indent=4)  # 缩进4字符
with open(path, 'w') as json_file:
    json_file.write(json_str)


length_file = json.load(open("/home/dingning/workspace/LaBERT_lengthpredict/id2length_train_max.json"))
print(len(length_file)) #118287
#print(length_file['522418']) #12
#print(length_file['184613']) #14
#print(length_file['318219']) #11
'''

gtf = json.load(open("/home/dingning/workspace/LaBERT_lengthpredict/id2length_test_aver.json"))
pref = json.load(open("/home/dingning/workspace/LaBERT_lengthpredict/output_4_lengthcls_mask_averlength/inference/model_0097500/caption_results_level.json"))
boundaries = ((7, 9), (10, 14), (15, 19), (20, 25))

print(len(gtf))
print(gtf['384213'])

print(len(pref))
print(pref['384213']['level'])

dict = {}
for pkey in pref.keys():
    plevel = pref[str(pkey)]['level']
    gtlevel = gtf[str(f'{int(pkey):06d}')]
    for i, (l, h) in enumerate(boundaries, 1):
        if l <= gtlevel <= h:
            gtlevel = i
    #print("plevel", plevel)
    #print('gtlevel', gtlevel)
    if int(plevel) == gtlevel:
        if plevel not in dict:
            dict[plevel] = 0
        dict[plevel] += 1
print(dict)


