# -*- coding: utf-8 -*-
# file: train.py
# brief: YOLOv3 implementation based on PyTorch
# author: Zeng Zhiwei
# date: 2019/7/18

import os
import torch
import random
import logging
import argparse
import numpy as np
import torch.utils.data
from progressbar import *
import multiprocessing as mp
from functools import partial
from collections import defaultdict

import utils
import yolov3
import darknet
import dataset as ds

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('--in-size', type=int, default=[416,416],
        nargs='+', help='network input size (width, height)')
    parser.add_argument('--num-classes', type=int, default=1,
        help='number of classes')
    parser.add_argument('--resume', help='resume training',
        action='store_true')
    parser.add_argument('--checkpoint', type=str, default='',
        help='checkpoint model file')
    parser.add_argument('--dataset', type=str, default='dataset',
        help='dataset path')
    parser.add_argument('--batch-size', type=int, default=8,
        help='training batch size')
    parser.add_argument('--accumulated-batches', type=int, default=1,
        help='update weights every accumulated batches')
    parser.add_argument('--scale-step', type=int, default=[320,608,10],
        nargs='+', help='scale step for multi-scale training')
    parser.add_argument('--rescale-freq', type=int, default=80,
        help='image rescaling frequency')
    parser.add_argument('--epochs', type=int, default=50,
        help='number of total epochs to run')
    parser.add_argument('--warmup', type=int, default=1000,
        help='warmup iterations')
    parser.add_argument('--workers', type=int, default=4,
        help='number of data loading workers')
    parser.add_argument('--optim', type=str, default='sgd',
        help='optimization algorithms, adam or sgd')
    parser.add_argument('--lr', type=float, default=0.0001,
        help='initial learning rate')
    parser.add_argument('--milestones', type=int, default=[-1,-1],
        nargs='+', help='list of batch indices, must be increasing')
    parser.add_argument('--lr-gamma', type=float, default=0.1,
        help='factor of decrease learning rate')
    parser.add_argument('--momentum', type=float, default=0.9,
        help='momentum')
    parser.add_argument('--weight-decay', type=float, default=0.0005,
        help='weight decay')
    parser.add_argument('--savename', type=str, default='yolov3',
        help='filename of trained model')
    parser.add_argument('--eval-epoch', type=int, default=10,
        help='epoch beginning evaluate')
    parser.add_argument('--sparsity', help='enable sparsity training',
        action='store_true')
    parser.add_argument('--lamb', type=float, default=0.01,
        help='sparsity factor')
    parser.add_argument('--pin', help='use pin_memory',
        action='store_true')
    parser.add_argument('--workspace', type=str, default='workspace',
        help='workspace path')
    parser.add_argument('--print-interval', type=int, default=40,
        help='log printing interval [40]')
    args = parser.parse_args()
    return args

def init_seeds(seed=0):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)

def get_logger(name='root', path=None):
    logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)
    if path is None:
        handler = logging.StreamHandler()
    else:
        handler = logging.FileHandler(path, encoding='utf-8')
    formatter = logging.Formatter(fmt='%(asctime)s [%(levelname)s]: %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger

def train(args):    
    utils.make_workspace_dirs(args.workspace)
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    anchors = np.loadtxt(os.path.join(args.dataset, 'anchors.txt'))
    scale_sampler = utils.TrainScaleSampler(args.in_size, args.scale_step,
        args.rescale_freq)
    shared_size = torch.IntTensor(args.in_size).share_memory_()
    
    torch.backends.cudnn.benchmark = True
    
    dataset = ds.CustomDataset(args.dataset, 'train')
    collate_fn = partial(ds.collate_fn, in_size=shared_size, train=True)
    data_loader = torch.utils.data.DataLoader(dataset, args.batch_size,
        True, num_workers=args.workers, collate_fn=collate_fn, pin_memory=args.pin, drop_last=True)

    num_ids = dataset.max_id + 2
    model = darknet.DarkNet(anchors, num_classes=args.num_classes, num_ids=num_ids).to(device)
    if args.checkpoint:
        print(f'load {args.checkpoint}')
        model.load_state_dict(torch.load(args.checkpoint))    
    
    params = [p for p in model.parameters() if p.requires_grad]
    if args.optim == 'sgd':
        optimizer = torch.optim.SGD(params, lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)
    else:
        optimizer = torch.optim.Adam(params, lr=args.lr, weight_decay=args.weight_decay)

    # freezon batch normalization layers
    for name, param in model.named_parameters():
        param.requires_grad = False if 'norm' in name else True
        if 'norm' in name: print(f'freeze {name}')

    trainer = f'{args.workspace}/checkpoint/trainer-ckpt.pth'
    if args.resume:
        trainer_state = torch.load(trainer)
        optimizer.load_state_dict(trainer_state['optimizer'])

    if -1 in args.milestones:
        num_batches = len(data_loader) * args.epochs
        args.milestones = [int(0.5 * num_batches), int(0.75 * num_batches)]
    
    def lr_lambda(iter):
        if iter < args.warmup:
            return pow(iter / args.warmup, 4)
        factor = 1
        for i in args.milestones:
            factor *= pow(args.lr_gamma, int(iter > i))
        return factor

    # lr_scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda)
    lr_scheduler = torch.optim.lr_scheduler.MultiStepLR(optimizer,
        milestones=[int(args.epochs * 0.5), int(args.epochs * 0.75)], gamma=args.lr_gamma)
    if args.resume:
        start_epoch = trainer_state['epoch'] + 1
        lr_scheduler.load_state_dict(trainer_state['lr_scheduler'])
    else:
        start_epoch = 0
        # lr_scheduler.step()     # set the lr for the first accumulated batches

    print(f'{args}\nStart training from epoch {start_epoch}')
    model_path = f'{args.workspace}/checkpoint/{args.savename}-ckpt-%03d.pth'
    logger = get_logger(path=os.path.join(args.workspace, 'log.txt'))
    size = shared_size.numpy().tolist()
    for epoch in range(start_epoch, args.epochs):
        model.train()
        logger.info(('%8s%10s%10s' + '%10s' * 8) % (
            'Epoch', 'Batch', 'SIZE', 'LBOX', 'LCLS', 'LIDE', 'LOSS', 'SB', 'SC', 'SI', 'LR'))

        rmetrics = defaultdict(float)
        optimizer.zero_grad()
        for batch, (images, targets) in enumerate(data_loader):
            warmup = min(args.warmup, len(data_loader))
            if epoch == 0 and batch <= warmup:
                lr = args.lr * (batch / warmup) ** 4
                for g in optimizer.param_groups:
                    g['lr'] = lr
        
            loss, metrics = model(images.to(device), targets.to(device), size)
            loss.backward()
            
            if args.sparsity:
                model.correct_bn_grad(args.lamb)
            
            num_batches = epoch * len(data_loader) + batch + 1
            if ((batch + 1) % args.accumulated_batches == 0) or (batch == len(data_loader) - 1):
                optimizer.step()
                optimizer.zero_grad()
                # lr_scheduler.step()

            for k, v in metrics.items():
                rmetrics[k] = (rmetrics[k] * batch + metrics[k]) / (batch + 1)
            
            fmt = tuple([('%g/%g') % (epoch, args.epochs), ('%g/%g') % (batch,
                len(data_loader)), ('%gx%g') % (size[0], size[1])] + \
                list(rmetrics.values()) + [optimizer.param_groups[0]['lr']])
            if batch % args.print_interval == 0:
                logger.info(('%8s%10s%10s' + '%10.3g' * (len(rmetrics.values()) + 1)) % fmt)

            size = scale_sampler(num_batches)
            shared_size[0], shared_size[1] = size[0], size[1]
      
        torch.save(model.state_dict(), f"{model_path}" % epoch)
        torch.save({'epoch' : epoch,
            'optimizer' : optimizer.state_dict(),
            'lr_scheduler' : lr_scheduler.state_dict()}, trainer)
        
        if epoch >= args.eval_epoch:
            pass
        lr_scheduler.step()

if __name__ == '__main__':
    args = parse_args()
    init_seeds()
    train(args)