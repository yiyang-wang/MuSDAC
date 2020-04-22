import torch
import torch.optim as optim
import torch.nn.functional as F
import argparse
import json
import math
import numpy as np
import random
from itertools import chain
from functools import reduce

from nets.layers import MaximumMeanDiscrepancy
from nets.models import MuCDAC, WeightedSummation, Discriminator
from utils.data_reader import load_data
from utils.tendency import plt_tendency, plt_compare
from utils.classifier import write_files, classify
from utils.process import acc, ConditionalMMD

# parser = argparse.ArgumentParser()
# parser.parse_args()

use_cuda = True
n_meta = 3

adjs_a, features_a, labels_a = [], torch.Tensor(), torch.Tensor()
adjs_b, features_b, labels_b = [], torch.Tensor(), torch.Tensor()
features_a *= 1e4
features_b *= 1e4

fea_dim = 0
hid1_dim = 64
hid2_dim = 32
emb_dim = 16
cls_dim = 0
mmd_ratio_ = 10
gan_ratio_ = 1
cmmd_ratio_ = 1
cod_ratio_ = 0.5
grow = 1

lr = 1e-3
eval_lr = 1e-2
wc = 5e-4
eval_wc = 0

seeds = [15, 16, 17, 18, 19]


# seeds = [19]


def load_dataset(src, tgt, meta=3):
    global adjs_a, features_a, labels_a, adjs_b, features_b, labels_b, fea_dim, cls_dim, n_meta
    n_meta = meta
    adjs_a, features_a, labels_a = load_data('data', src, n_meta)
    adjs_b, features_b, labels_b = load_data('data', tgt, n_meta)
    features_a *= 1e4
    features_b *= 1e4
    fea_dim = features_a.shape[-1]
    cls_dim = labels_a.max().item() + 1
    if use_cuda:
        features_a = features_a.cuda()
        features_b = features_b.cuda()
        labels_a = labels_a.cuda()
        labels_b = labels_b.cuda()
        for i in range(n_meta):
            adjs_a[i] = adjs_a[i].cuda()
            adjs_b[i] = adjs_b[i].cuda()


def set_seed(seed: int):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if use_cuda:
        torch.cuda.manual_seed(seed)


def eval_channel(mask: tuple, epochs=50, kernel_num=5) -> float:
    model = MuCDAC(fea_dim, hid1_dim, hid2_dim, emb_dim, cls_dim, len(mask), 'f')
    mmd = MaximumMeanDiscrepancy(kernel_num=kernel_num)
    optimizer = optim.Adam(model.parameters(), lr=eval_lr, weight_decay=eval_wc)
    if use_cuda:
        model.cuda()

    def train_one(epoch_rate: float, print_mode=True) -> float:
        ratio = (2 / (1 + math.exp(-grow * epoch_rate)) - 1)
        mmd_ratio = ratio * mmd_ratio_
        # print('mmd_ratio:', mmd_ratio)
        optimizer.zero_grad()
        model.set_adjs(model.select(adjs_a, mask))
        embs_a, preds_a = model(features_a)
        model.set_adjs(model.select(adjs_b, mask))
        embs_b, preds_b = model(features_b)

        losses = []
        for src, tgt, pred_a, pred_b in zip(embs_a, embs_b, preds_a, preds_b):
            ls = F.nll_loss(pred_a, labels_a) + mmd_ratio * mmd(src, tgt)
            losses.append(ls)
        loss = sum(losses)
        if print_mode:
            print('loss:', loss.cpu().item())
        loss.backward()
        optimizer.step()
        return loss.cpu().item()

    loss = 0
    for e in range(epochs):
        # print('epoch:', e)
        loss = train_one(e / epochs, print_mode=e == epochs - 1)

    return loss


def heuristics_channel_combinations_selection(kernel_num=5) -> list:
    def binary(com: tuple) -> int:
        bi = 0
        for c in com:
            bi |= 1 << c
        return bi

    def con_binary(bi: int) -> tuple:
        b = 0
        com = []
        while bi:
            if bi % (1 << (b + 1)):
                bi -= (1 << b)
                com.append(b)
            b += 1
        return tuple(com)

    queue = [binary((i,)) for i in range(n_meta)]
    single_loss = []
    loss_map = {}
    for i in range(n_meta):
        bi_loss = [(x, eval_channel(con_binary(x), kernel_num=kernel_num)) for x in queue]
        if i == 0:
            single_loss.extend(bi_loss)
        for bi, loss in bi_loss:
            loss_map[bi] = loss

        if i == n_meta - 1:
            break
        assess_loss_map = {}
        for bi, loss in bi_loss:
            for c, loss1 in single_loss:
                new_bi = bi | c
                if new_bi == bi:
                    continue
                new_loss = (loss * (i + 1) + loss1) / (i + 2)
                try:
                    if assess_loss_map[new_bi] > new_loss:
                        assess_loss_map[new_bi] = new_loss
                except KeyError:
                    assess_loss_map[new_bi] = new_loss

        assess_loss = sorted(assess_loss_map.items(), key=lambda x: x[1])
        queue = [bi for bi, _ in assess_loss[:n_meta - i - 1]]

    total_bi_loss = sorted(loss_map.items(), key=lambda x: x[1])
    print(total_bi_loss)
    total_com = [con_binary(bi) for bi, _ in total_bi_loss][:2 * n_meta - 1]
    return total_com


def train(epochs=200, cu=None, tag='', directory='temp/', print_mode=False) -> str:
    model = MuCDAC(fea_dim, hid1_dim, hid2_dim, emb_dim, cls_dim, n_meta, cu)
    discriminators = [Discriminator(emb_dim) for _ in range(model.comps_num)]
    summation = WeightedSummation(model.comps_num, False)

    gen_optimizer = optim.Adam(model.parameters(), lr=lr, weight_decay=wc)
    dis_optimizer = optim.Adam(reduce(lambda x, y: chain(x, y), map(lambda x: x.parameters(), discriminators)))

    if use_cuda:
        model.cuda()
        for discriminator in discriminators:
            discriminator.cuda()
        summation.cuda()

    def train_dis():
        model.eval()
        for discriminator in discriminators:
            discriminator.train()
        dis_optimizer.zero_grad()

        model.set_adjs(adjs_a)
        embs_a, _ = model(features_a)
        model.set_adjs(adjs_b)
        embs_b, _ = model(features_b)

        losses_gan = []
        for emb_a, emb_b, discriminator in zip(embs_a, embs_b, discriminators):
            x = torch.cat([emb_a, emb_b], 0)
            y = torch.zeros(emb_a.shape[0] + emb_b.shape[0], dtype=torch.float32)
            y[:emb_a.shape[0]] = 1 - 0.01
            y[emb_a.shape[0]:] = 0.01
            if use_cuda:
                y = y.cuda()

            losses_gan.append(F.binary_cross_entropy(discriminator(x), y))

        loss_gan = sum(losses_gan)
        loss_gan.backward()
        dis_optimizer.step()

    def train_gen(epoch_rate: float, print_mode=True) -> (float, float):
        model.train()
        for discriminator in discriminators:
            discriminator.eval()
        gen_optimizer.zero_grad()

        ratio = (2 / (1 + math.exp(-grow * epoch_rate)) - 1)
        gan_ratio = ratio * gan_ratio_
        model.set_adjs(adjs_a)
        embs_a, preds_a = model(features_a)
        model.set_adjs(adjs_b)
        embs_b, preds_b = model(features_b)

        if print_mode:
            print(F.softmax(summation.theta, dim=2).cpu().data)

        losses = []
        for src, tgt, pred_a, pred_b, discriminator in zip(embs_a, embs_b, preds_a, preds_b, discriminators):
            x = torch.cat([src, tgt], 0)
            y = torch.zeros(src.shape[0] + tgt.shape[0], dtype=torch.float32)
            y[:src.shape[0]] = 0.01
            y[src.shape[0]:] = 1 - 0.01
            if use_cuda:
                y = y.cuda()
            ls = F.nll_loss(pred_a, labels_a) + gan_ratio * F.binary_cross_entropy(discriminator(x), y)
            losses.append(ls)
        if print_mode:
            print('losses:', [l.cpu().item() for l in losses])
        loss = summation(torch.stack(losses).unsqueeze(-1).unsqueeze(0)).sum()
        loss.backward()
        gen_optimizer.step()

        pred_a = summation(torch.transpose(torch.stack(preds_a), 0, 1))
        pred_b = summation(torch.transpose(torch.stack(preds_b), 0, 1))
        a_src = acc(pred_a, labels_a)
        a_tgt = acc(pred_b, labels_b)
        as_src = [acc(pred, labels_a) for pred in preds_a]
        as_tgt = [acc(pred, labels_b) for pred in preds_b]
        if print_mode:
            print('src:', a_src)
            print('tgt:', a_tgt)
            print(as_src)
            print(as_tgt)

        return a_src, a_tgt

    file = 'result_{}.txt'.format(tag)
    f = open(directory + file, 'w+')
    print('write file: {}'.format(file))
    for e in range(epochs):
        # print('epoch:', e)
        for _ in range(5):
            train_dis()
        a_s, a_t = train_gen(e / epochs, print_mode=e == epochs - 1 or print_mode)
        f.write(json.dumps({'epoch': e, 'ac_src': a_s, 'ac_tgt': a_t}) + '\n')

    f.close()

    # plt_tendency(file, tag, directory)
    return file


if __name__ == '__main__':
    for source, target, meta_tag in [
        ('acm_4_1500_a', 'acm_4_1500_b', 'acm-ab'),
        ('acm_4_1500_b', 'acm_4_1500_a', 'acm-ba'),
        # ('slap_4_2000_a', 'slap_4_2000_b', 'slap-ab'),
        # ('slap_4_2000_b', 'slap_4_2000_a', 'slap-ba'),
        ('am_4_1500_a', 'am_4_1500_b', 'am-ab'),
        ('am_4_1500_b', 'am_4_1500_a', 'am-ba'),
        ('dblp_4_1500_a', 'dblp_4_1500_b', 'dblp-ab'),
        ('dblp_4_1500_b', 'dblp_4_1500_a', 'dblp-ba'),
    ]:
        meta = 6 if meta_tag.startswith('slap') else 3
        load_dataset(source, target, meta=meta)
        kn = 2 if meta_tag.startswith('am') else 5
        for seed in seeds:
            print('----- for seed {} -----'.format(seed))
            set_seed(seed)
            directory = 'temp/{}/'.format(seed)
            # res_fus = train(tag='fus-am-ba', cu='f', avg=True, directory=directory)
            # res_cod = train(tag='cod-am-ab', voting='cod', directory=directory)
            # res_dic = train(tag='dic-am-ab', voting='dic', directory=directory)
            # res_no_mmd = train(tag='no_mmd-dblp-ab', use_mmd=False, directory=directory)
            # res_mmd = train(tag='mmd-am-ab', directory=directory)

            # cod_ratio_ = 0.1
            # res_cod = train(tag='urf-cod01-' + meta_tag, voting='cod', directory=directory, cu='urf', kernel_num=kn)
            # cod_ratio_ = 1.0
            # res_cod = train(tag='urf-cod1-' + meta_tag, voting='cod', directory=directory, cu='urf', kernel_num=kn)
            # cod_ratio_ = 2.0
            # res_cod = train(tag='urf-cod2-' + meta_tag, voting='cod', directory=directory, cu='urf', kernel_num=kn)
            #
            # mmd_ratio_ = 1
            # res_mmd = train(tag='urf-mmd1-' + meta_tag, directory=directory, cu='urf', kernel_num=kn)
            # mmd_ratio_ = 5
            # res_mmd = train(tag='urf-mmd5-' + meta_tag, directory=directory, cu='urf', kernel_num=kn)
            # mmd_ratio_ = 20
            # res_mmd = train(tag='urf-mmd20-' + meta_tag, directory=directory, cu='urf', kernel_num=kn)

            # res_urf = train(tag='urf-mmd-' + meta_tag, directory=directory, cu='urf', kernel_num=kn)
            # res_rdm = train(tag='rdm-mmd-' + meta_tag, directory=directory, cu='rdm', kernel_num=kn)
            # res_alc = train(tag='alc-mmd-' + meta_tag, directory=directory, cu='alc', kernel_num=kn)
            hrs_cu = heuristics_channel_combinations_selection()
            # cod_ratio_ = 0.1
            # train(tag='hrs-cod01-' + meta_tag, voting='cod', directory=directory, cu=hrs_cu, kernel_num=kn)
            # cod_ratio_ = 1.0
            # train(tag='hrs-cod1-' + meta_tag, voting='cod', directory=directory, cu=hrs_cu, kernel_num=kn)
            # cod_ratio_ = 2.0
            # train(tag='hrs-cod2-' + meta_tag, voting='cod', directory=directory, cu=hrs_cu, kernel_num=kn)
            # cod_ratio_ = 0.5
            #
            # mmd_ratio_ = 1
            # train(tag='hrs-mmd1-' + meta_tag, directory=directory, cu=hrs_cu, kernel_num=kn)
            # mmd_ratio_ = 5
            # train(tag='hrs-mmd5-' + meta_tag, directory=directory, cu=hrs_cu, kernel_num=kn)
            # mmd_ratio_ = 20
            # train(tag='hrs-mmd20-' + meta_tag, directory=directory, cu=hrs_cu, kernel_num=kn)
            # mmd_ratio_ = 10

            res_gan = train(tag='hrs-gan-' + meta_tag, directory=directory, cu=hrs_cu)

            # res_hrs = train(tag='hrs-mmd-' + meta_tag, directory=directory, cu=hrs_cu, kernel_num=kn)
            # res_avg = train(tag='hrs-avg-' + meta_tag, avg=True, directory=directory, cu=hrs_cu, kernel_num=kn)
            # res_cod = train(tag='hrs-cod-' + meta_tag, voting='cod', directory=directory, cu=hrs_cu, kernel_num=kn)
            # res_dic = train(tag='hrs-dic-' + meta_tag, voting='dic', directory=directory, cu=hrs_cu, kernel_num=kn)
            # res_nom = train(tag='hrs-nom-' + meta_tag, use_mmd=False, directory=directory, cu=hrs_cu, kernel_num=kn)
            # res_avg = train(tag='urf-avg-' + meta_tag, avg=True, directory=directory, cu='urf', kernel_num=kn)
            # res_mmd = train(tag='urf-mmd-' + meta_tag, directory=directory, cu='urf', kernel_num=kn)
            # res_cod = train(tag='urf-cod-' + meta_tag, voting='cod', directory=directory, cu='urf', kernel_num=kn)
            # res_dic = train(tag='urf-dic-' + meta_tag, voting='dic', directory=directory, cu='urf', kernel_num=kn)
            # res_nom = train(tag='urf-nom-' + meta_tag, use_mmd=False, directory=directory, cu='urf', kernel_num=kn)
            # res_cmmd = train(tag='cmmd-acm-ab', conditional=True, directory=directory)
            # res_avg = train(tag='avg-am-ab', avg=True, directory=directory)
            # res_no_mmd_unique = train(tag='no_mmd_unique-acm-ab', use_mmd=False, cu='u', directory=directory)
            # res_unique = train(tag='mmd_unique-dblp-ba', cu='u', directory=directory)
            # res_common = train(tag='mmd_common-dblp-ba', cu='c', directory=directory)
            # plt_compare([res_no_mmd, res_mmd, res_cmmd, res_avg, res_no_mmd_unique, res_unique, res_common],
            #             tag='acm-ba', directory=directory)
