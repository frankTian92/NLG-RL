from data import Corpus
from model import Embedding
from model import EncDec
from model import VocGenerator
import utils

import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.autograd import Variable

import random
import math
import os
import time
import sys

import numpy as np

import argparse

parser = argparse.ArgumentParser(description='Pre-training machine translation model with or without vocabulary prediction')

parser.add_argument('--seed', type = int, default = 1,
                    help='Random seed')
parser.add_argument('--gpu', type = int, default = 0,
                    help='GPU id')

parser.add_argument('--train_source', type = str, required = True,
                    help = 'File path to training data (source sentences)')
parser.add_argument('--train_source_orig', type = str, default = None,
                    help = 'File path to case-sensitive training data, if applicable (source sentences)')
parser.add_argument('--train_target', type = str, required = True,
                    help = 'File path to training data (target sentences)')

parser.add_argument('--dev_source', type = str, required = True,
                    help = 'File path to development data (source sentences)')
parser.add_argument('--dev_source_orig', type = str, default = None,
                    help = 'File path to case-sensitive development data, if applicable (source sentences)')
parser.add_argument('--dev_target', type = str, required = True,
                    help = 'File path to development data (target sentences)')

parser.add_argument('--model_vocgen', type = str, default = './params/vocgen.bin',
                    help = 'File name for loading model parameters of trained vocabulary predictor')
parser.add_argument('--model_nmt', type = str, default = './params/nmt.bin',
                    help = 'File name for saving model parameters')

parser.add_argument('--trans_file', type = str, default = './trans.txt',
                    help = 'Temporary file to output model translations of development data')
parser.add_argument('--gold_file', type = str, default = './gold.txt',
                    help = 'Temporary file to output gold-standard translations of development data')
parser.add_argument('--bleu_file', type = str, default = './bleu.txt',
                    help = 'Temporary file to output BLEU score')

parser.add_argument('--fs', type = int, default = '2',
                    help = 'Minimum word frequency to construct source vocabulary')
parser.add_argument('--ft', type = int, default = '2',
                    help = 'Minimum word frequency to construct target vocabulary')
parser.add_argument('--mlen', type = int, default = '100',
                    help = 'Maximum length of sentences in training data')

parser.add_argument('--K', type = int, default = '1000',
                    help = 'Small vocabulary size for NMT model training (Full softmax if K <= 0 or K > target vocabulary size)')
parser.add_argument('--dim_vocgen', type = int, default = '512',
                    help = 'Dimensionality for embeddings and hidden states of vocabulary predictor')
parser.add_argument('--dim_nmt', type = int, default = '256',
                    help = 'Dimensionality for embeddings and hidden states of NMT model')
parser.add_argument('--layers', type = int, default = '1',
                    help = 'Number of LSTM layers (currently, 1 or 2)')
parser.add_argument('--mepoch', type = int, default = '20',
                    help = 'Maximum number of training epochs')
parser.add_argument('--lr', type = float, default = '1.0',
                    help = 'Learning rate for SGD')
parser.add_argument('--momentum', type = float, default = '0.75',
                    help = 'Momentum rate for SGD')
parser.add_argument('--lrd', type = float, default = '0.5',
                    help = 'Learning rate decay for AdaGrad')
parser.add_argument('--bs', type = int, default = '128',
                    help = 'Batch size')
parser.add_argument('--dp', type = float, default = '0.2',
                    help = 'Dropout rate for NMT model')
parser.add_argument('--wd', type = float, default = '1.0e-06',
                    help = 'Weight decay rate for internal weight matrices')
parser.add_argument('--clip', type = float, default = '1.0',
                    help = 'Clipping value for gradient norm')

args = parser.parse_args()
print(args)

sourceDevFile = args.dev_source
sourceOrigDevFile = (sourceDevFile if args.dev_source_orig is None else args.dev_source_orig)
targetDevFile = args.dev_target

sourceTrainFile = args.train_source
sourceOrigTrainFile = (sourceTrainFile if args.train_source_orig is None else args.train_source_orig)
targetTrainFile = args.train_target

vocGenFile = args.model_vocgen
nmtFile = args.model_nmt

transFile = args.trans_file
goldFile = args.gold_file
bleuFile = args.bleu_file

minFreqSource = args.fs
minFreqTarget = args.ft
hiddenDim = args.dim_nmt
decay = args.lrd
gradClip = args.clip
dropoutRate = args.dp
numLayers = args.layers
    
maxLen = args.mlen
maxEpoch = args.mepoch
decayStart = 5

sourceEmbedDim = hiddenDim
targetEmbedDim = hiddenDim

vocGenHiddenDim = args.dim_vocgen

batchSize = args.bs

learningRate = args.lr
momentumRate = args.momentum

gpuId = args.gpu
seed = args.seed

weightDecay = args.wd

K = args.K

torch.set_num_threads(1)

torch.manual_seed(seed)
random.seed(seed)
torch.cuda.set_device(gpuId)
torch.cuda.manual_seed(seed)

corpus = Corpus(sourceTrainFile, sourceOrigTrainFile, targetTrainFile,
                sourceDevFile, sourceOrigDevFile, targetDevFile,
                minFreqSource, minFreqTarget, maxLen)
    
print('Source vocabulary size: '+str(corpus.sourceVoc.size()))
print('Target vocabulary size: '+str(corpus.targetVoc.size()))
print()
print('# of training samples: '+str(len(corpus.trainData)))
print('# of develop samples:  '+str(len(corpus.devData)))
print('Random seed: ', str(seed))

useSmallSoftmax = (K > 0 and K <= corpus.targetVoc.size())

if useSmallSoftmax:
    print('K = ', K)
else:
    print('Full softmax')
print()

embedding = Embedding(sourceEmbedDim, targetEmbedDim, corpus.sourceVoc.size(), corpus.targetVoc.size())
encdec = EncDec(sourceEmbedDim, targetEmbedDim, hiddenDim,
                corpus.targetVoc.size(), useSmallSoftmax = useSmallSoftmax, dropoutRate = dropoutRate, numLayers = numLayers)

if useSmallSoftmax:
    vocGen = VocGenerator(vocGenHiddenDim, corpus.targetVoc.size(), corpus.sourceVoc.size())
    vocGen.load_state_dict(torch.load(vocGenFile))
    vocGen.cuda()
    vocGen.eval()

encdec.softmaxLayer.weight.weight = embedding.targetEmbedding.weight

'''
# how to load NMT model parameters
all_state_dict = torch.load(nmtFile)
embedding_state_dict = {}
encdec_state_dict = {}
for s in all_state_dict:
    if 'Embedding' in s:
        embedding_state_dict[s] = all_state_dict[s]
    else:
        encdec_state_dict[s] = all_state_dict[s]
embedding.load_state_dict(embedding_state_dict)
encdec.load_state_dict(encdec_state_dict)
'''


embedding.cuda()
encdec.cuda()

batchListTrain = utils.buildBatchList(len(corpus.trainData), batchSize)
batchListDev = utils.buildBatchList(len(corpus.devData), batchSize)

withoutWeightDecay = []
withWeightDecay = []
for name, param in list(embedding.named_parameters())+list(encdec.named_parameters()):
    if 'bias' in name or 'Embedding' in name:
        withoutWeightDecay += [param]
    elif 'softmax' not in name:
        withWeightDecay += [param]
optParams = [{'params': withWeightDecay, 'weight_decay': weightDecay},
             {'params': withoutWeightDecay, 'weight_decay': 0.0}]
totalParamsNMT = withoutWeightDecay+withWeightDecay
opt = optim.SGD(optParams, momentum = momentumRate, lr = learningRate)

bestDevGleu = -1.0
prevDevGleu = -1.0

if useSmallSoftmax:

    print('Pre-computing small vocabularies (requires CPU memory)...')
    for batch in batchListDev:
        batchSize = batch[1]-batch[0]+1

        batchInputSource, lengthsSource, batchInputTarget, batchTarget, lengthsTarget, tokenCount, batchData, maxTargetLen = corpus.processBatchInfoNMT(batch, train = False, volatile = True)

        targetVocGen, inputVocGen = corpus.processBatchInfoVocGen(batchData, smoothing = False)
        outputVocGen = vocGen(inputVocGen)

        tmp = F.sigmoid(outputVocGen.data).data
        val, output_list = torch.topk(tmp, k = K)
        output_list = output_list.cpu()

        for i in range(batchSize):
            batchData[i].smallVoc = output_list[i]

    for batch in batchListTrain:
        batchSize = batch[1]-batch[0]+1

        batchInputSource, lengthsSource, batchInputTarget, batchTarget, lengthsTarget, tokenCount, batchData, maxTargetLen = corpus.processBatchInfoNMT(batch, train = True, volatile = True)

        targetVocGen, inputVocGen = corpus.processBatchInfoVocGen(batchData, smoothing = False)
        outputVocGen = vocGen(inputVocGen)

        tmp = F.sigmoid(outputVocGen.data).data+targetVocGen.data
        tmp[:, corpus.targetVoc.unkIndex] = 1.0
        val, output_list = torch.topk(tmp, k = K)
        output_list = output_list.cpu()

        for i in range(batchSize):
            batchData[i].smallVoc = output_list[i]
    print('Done.\n')

for epoch in range(maxEpoch):

    batchProcessed = 0
    totalLoss = 0.0
    totalTrainTokenCount = 0.0
    
    print('--- Epoch ' + str(epoch+1))
    startTime = time.time()
    
    random.shuffle(corpus.trainData)

    embedding.train()
    encdec.train()

    for batch in batchListTrain:
        print('\r', end = '')
        print(batchProcessed+1, '/', len(batchListTrain), end = '')

        batchSize = batch[1]-batch[0]+1
        
        batchInputSource, lengthsSource, batchInputTarget, batchTarget, lengthsTarget, tokenCount, batchData, maxTargetLen = corpus.processBatchInfoNMT(batch, train = True)
        
        inputSource = embedding.getBatchedSourceEmbedding(batchInputSource)
        sourceH, (hn, cn) = encdec.encode(inputSource, lengthsSource)
        
        if useSmallSoftmax:
            output_list = torch.LongTensor(batchSize, K)
            for i in range(batchSize):
                output_list[i] = batchData[i].smallVoc
            
            utils.convertTargetIndex(batchSize, batchInputTarget.data.numpy(), output_list.numpy(), np.array(lengthsTarget).astype(int), batchTarget.data.numpy(), maxTargetLen, corpus.targetVoc.eosIndex)
            
            output_list = Variable(output_list, requires_grad = False).cuda()
        else:
            output_list = None

        batchInputTarget = batchInputTarget.cuda()
        batchTarget = batchTarget.cuda()
        inputTarget = embedding.getBatchedTargetEmbedding(batchInputTarget)
        output = encdec(inputTarget, lengthsTarget, lengthsSource, (hn, cn), sourceH, output_list)
            
        loss = encdec.softmaxLayer.computeLoss(output, batchTarget)
        totalLoss += loss.data[0]
        totalTrainTokenCount += tokenCount
        loss /= batchSize
            
        opt.zero_grad()
        loss.backward()
        nn.utils.clip_grad_norm(totalParamsNMT, gradClip)
        opt.step()

        batchProcessed += 1
        if True or batchProcessed == len(batchListTrain)//2 or batchProcessed == len(batchListTrain):
            devPerp = 0.0
            devGleu = 0.0
            totalTokenCount = 0.0

            embedding.eval()
            encdec.eval()

            print()
            print('Training time: ' + str(time.time()-startTime) + ' sec')
            print('Train perp: ' + str(math.exp(totalLoss/totalTrainTokenCount)))
            
            f_trans = open(transFile, 'w')
            f_gold = open(goldFile, 'w')
            
            for batch in batchListDev:
                batchSize = batch[1]-batch[0]+1
                batchInputSource, lengthsSource, batchInputTarget, batchTarget, lengthsTarget, tokenCount, batchData, maxTargetLen = corpus.processBatchInfoNMT(batch, train = False, volatile = True)

                inputSource = embedding.getBatchedSourceEmbedding(batchInputSource)
                sourceH, (hn, cn) = encdec.encode(inputSource, lengthsSource)

                if useSmallSoftmax:
                    output_list = torch.LongTensor(batchSize, K)
                    for i in range(batchSize):
                        output_list[i] = batchData[i].smallVoc
                    
                    output_list = Variable(output_list, requires_grad = False, volatile = True).cuda()
                    encdec.softmaxLayer.setSubset(output_list)
                    indicesGreedy, lengthsGreedy, attentionIndices = encdec.sample(corpus.targetVoc.bosIndex, corpus.targetVoc.eosIndex, lengthsSource, embedding.targetEmbedding, sourceH, (hn, cn), useSmallSoftmax = True, output_list = output_list.cpu(), greedyProb = 1.0, maxGenLen = maxLen)
                else:
                    indicesGreedy, lengthsGreedy, attentionIndices = encdec.sample(corpus.targetVoc.bosIndex, corpus.targetVoc.eosIndex, lengthsSource, embedding.targetEmbedding, sourceH, (hn, cn), greedyProb = 1.0, maxGenLen = maxLen)
                
                indicesGreedy = indicesGreedy.cpu()

                for i in range(batchSize):
                    for k in range(lengthsGreedy[i]-1):
                        index = indicesGreedy.data[i, k]
                        if index == corpus.targetVoc.unkIndex:
                            index = attentionIndices[i, k]
                            f_trans.write(batchData[i].sourceOrigStr[index] + ' ')
                        else:
                            f_trans.write(corpus.targetVoc.tokenList[index].str + ' ')
                    f_trans.write('\n')

                    for k in range(lengthsTarget[i]-1):
                        index = batchInputTarget.data[i, k+1]
                        if index == corpus.targetVoc.unkIndex:
                            f_gold.write(batchData[i].targetUnkMap[k] + ' ')
                        else:
                            f_gold.write(corpus.targetVoc.tokenList[index].str + ' ')
                    f_gold.write('\n')
                
            f_trans.close()
            f_gold.close()
            os.system("./bleu.sh " + goldFile + " " + transFile + " " + bleuFile + " 2> DUMMY")
            f_trans = open(bleuFile, 'r')
            for line in f_trans:
                devGleu = float(line.split()[2][0:-1])
                break
            f_trans.close()
            
            print("Dev BLEU:", devGleu)
            
            embedding.train()
            encdec.train()

            if epoch > decayStart and devGleu < prevDevGleu:
                print('lr -> ' + str(learningRate*decay))
                learningRate *= decay

                for paramGroup in opt.param_groups:
                    paramGroup['lr'] = learningRate
                
            elif devGleu >= bestDevGleu:
                bestDevGleu = devGleu

                stateDict = embedding.state_dict().copy()
                stateDict.update(encdec.state_dict())
                for elem in stateDict:
                    stateDict[elem] = stateDict[elem].cpu()
                torch.save(stateDict, nmtFile)
                
            prevDevGleu = devGleu

