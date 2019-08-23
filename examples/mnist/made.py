import argparse
import pprint
import pathlib
import numpy as np
import torch
import json
from tqdm import tqdm
from collections import OrderedDict, defaultdict

from torch.distributions import Bernoulli

import dgm
from dgm.conditional import MADEConditioner
from dgm.likelihood import FullyFactorizedLikelihood, AutoregressiveLikelihood
from dgm.opt_utils import get_optimizer, ReduceLROnPlateau
from utils import load_mnist, Batcher


def config(**kwargs):
    """You can use kwargs to programmatically overwrite existing parameters"""

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    # Data
    parser.add_argument('--height', type=int, default=28,
        help='Used to specify the data_dim=height*width')
    parser.add_argument('--width', type=int, default=28,
        help='Used to specify the data_dim=height*width')
    parser.add_argument('--binarize', type=bool, default=True)
    parser.add_argument('--batch_size', type=int, default=64)

    # Model and Architecture
    parser.add_argument('--conditional', default=False, action="store_true",
        help="Model P(x|y), i.e. P(digit|class).")
    parser.add_argument('--distribution', type=str, default='bernoulli',
        help='Data likelihood',
        choices=['bernoulli']
    )
    parser.add_argument('--num_masks', type=int, default=1, help="Use k > 1 for k random permutation masks.")
    parser.add_argument('--hidden_sizes', type=int, default=[500, 500])
    parser.add_argument('--resample_mask_every', type=int, default=20,
        help='Resample mask every so often to make training agnostic to order of variables.'
    )


    # Optimization
    parser.add_argument('--epochs', type=int, default=100)
    parser.add_argument('--input_dropout', type=float, default=0.)
    parser.add_argument('--gen_opt', type=str, default="adam")
    parser.add_argument('--gen_lr', type=float, default=1e-4)
    parser.add_argument('--gen_l2_weight', type=float, default=1e-4)
    parser.add_argument('--gen_momentum', type=int, default=0.)
    parser.add_argument('--patience', type=int, default=10)
    parser.add_argument('--early_stopping', type=int, default=10)


    # Metrics
    parser.add_argument('--ll_samples',
        type=int, default=10,
        help='Ensemble a number of random masks for dev/test NLL')

    # Experiment
    parser.add_argument('--data_dir', type=str, default='./data')
    parser.add_argument('--output_dir', type=str, default='./runs')
    parser.add_argument('--device', type=str, default='cuda:0')
    parser.add_argument('--logdir', type=str, default=None,
        help='Tensorboard logdir')
    parser.add_argument('--seed', type=int, default=42)

    args, _ = parser.parse_known_args()
    # overwrites
    for k, v in kwargs.items():
        args.__dict__[k] = v
    
    # Save hyperparameters
    output_dir = pathlib.Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)        
    with open(output_dir/"hparams", "w") as f:
        json.dump(args.__dict__, f, sort_keys=True, indent=4)    
    
    # Output dir
    args.output_dir = output_dir
    
    # Log dir
    if args.logdir:
        args.logdir = pathlib.Path(args.logdir)
        args.logdir.mkdir(parents=True, exist_ok=True)    

    # CPU/CUDA device
    args.device = torch.device(args.device) 
    args.device
    
    # reproducibility is good
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.cuda.manual_seed_all(args.seed)
    
    return args


def config_from_file(filename):
    with open(filename) as f:
        hparams = json.load(f)
    return config(**hparams)


def save_model(model, optimizer, args):
    def f():
        print('Saving model..')
        torch.save({
            'model': model.state_dict(),
            'opt': optimizer.state_dict(),
        }, args.output_dir/'checkpoint.pt')
    return f


def load_model(model, optimizer, args):
    def f():
        print('Loading model..')
        checkpoint = torch.load(args.output_dir/'checkpoint.pt')
        model.load_state_dict(checkpoint['model'])
        optimizer.load_state_dict(checkpoint['opt'])
    return f


def validate(batcher: Batcher, args, model,
    optimizer=None, scheduler=None, writer=None, name='dev'): 
    """
    :return: stop flag, dict 
        NLL can be found in the dict
    """

    if args.num_masks == 1:
        resample_mask = False
        num_samples = 1
    else:  # ensemble
        resample_mask = True
        num_samples = args.ll_samples    
    
    with torch.no_grad():
        model.eval()
        print_ = defaultdict(list)
        nb_instances = 0.
        for x_mb, y_mb in batcher:
            # [B, H*W]
            x_mb = x_mb.reshape(-1, args.height * args.width)            
            # [B, 10]
            made_inputs = x_mb if not args.conditional else torch.cat([x_mb, y_mb.float()], -1)
            # [B, H*W]
            p_x = model(
                inputs=y_mb.float() if args.conditional else None, 
                history=x_mb, 
                num_samples=num_samples, resample_mask=resample_mask
            )
            # [B]            
            nll = -p_x.log_prob(x_mb).sum(-1)
            # accumulate metrics
            print_['NLL'].append(nll.sum().item())
            nb_instances += x_mb.size(0)

        return_dict = {k: np.sum(v) / nb_instances for k, v in print_.items()}
        if writer:            
            writer.add_scalar('%s/NLL' % name, return_dict['NLL'])    

        stop = False
        if scheduler is not None:
            stop = scheduler.step(return_dict['NLL'],
                    callback_best=save_model(model, optimizer, args),
                    callback_reduce=load_model(model, optimizer, args))
                
        return stop, return_dict        


class Experiment:
    """
    Use this class to
    * load a dataset
    * build model and optimizer
    * get a batcher
    * train a model
    * load a trained model
    """
    
    def __init__(self, args):
        
        print("\n# Hyperparameters")
        pprint.pprint(args.__dict__)

        print("\n# Data")
        print(" - MNIST")    
        print(" - digit_dim=%d*%d" % (args.height, args.width))
        print(" - data_dim=%d*%d" % (args.height, args.width))
        train_loader, valid_loader, test_loader = load_mnist(
            args.batch_size, 
            save_to='{}/std/{}x{}'.format(args.data_dir, args.height, args.width),
            height=args.height, 
            width=args.width)

        print("\n# Generative model")
        print(" - binary outputs:", args.binarize)
        print(" - distribution:", args.distribution)
        print(" - conditional:", args.conditional)        
        x_size = args.width * args.height
        y_size = 10 if args.conditional else 0
        if args.distribution == 'bernoulli':
            if not args.binarize:
                raise ValueError("--distribution bernoulli requires --binarize True")
            made = MADEConditioner(
                input_size=x_size + y_size, 
                output_size=x_size * 1, 
                context_size=y_size,
                hidden_sizes=args.hidden_sizes,
                num_masks=args.num_masks
            )        
            model = AutoregressiveLikelihood(
                event_size=x_size,
                dist_type=Bernoulli, 
                conditioner=made
            ).to(args.device)
        else:
            raise ValueError("I do not know this likelihood: %s" % args.distribution)

        print("\n# Architecture")
        print(model)

        print("\n# Optimizer")
        gen_opt = get_optimizer(args.gen_opt, model.parameters(), args.gen_lr, args.gen_l2_weight, args.gen_momentum)
        gen_scheduler = ReduceLROnPlateau(
            gen_opt, 
            factor=0.5, 
            patience=args.patience,
            early_stopping=args.early_stopping,
            mode='min', threshold_mode='abs')
        print(gen_opt)
        
        self.train_loader = train_loader
        self.valid_loader = valid_loader
        self.test_loader = test_loader
        self.model = model
        self.gen_opt = gen_opt
        self.gen_scheduler = gen_scheduler
        self.args = args
        
    def get_batcher(self, data_loader):
        batcher = Batcher(
            data_loader, 
            height=self.args.height, 
            width=self.args.width, 
            device=self.args.device, 
            binarize=self.args.binarize, 
            num_classes=10,
            onehot=True
        )
        return batcher
    
    def load(self):
        load_model(self.model, self.gen_opt, self.args)()
        
    def train(self):
        print("\n# Training")
        args = self.args
        model, gen_opt, gen_scheduler = self.model, self.gen_opt, self.gen_scheduler

        if args.logdir:
            from tensorboardX import SummaryWriter        
            writer = SummaryWriter(args.logdir)
        else:
            writer = None

        step = 1

        for epoch in range(args.epochs):

            iterator = tqdm(self.get_batcher(self.train_loader))

            for x_mb, y_mb in iterator:
                # [B, H*W]
                x_mb = x_mb.reshape(-1, args.height * args.width)
                # [B, 10]
                context = y_mb.float() if args.conditional else None
                model.train()
                gen_opt.zero_grad()

                if args.num_masks == 1:
                    resample_mask = False
                else:  # training with variable masks
                    resample_mask = args.resample_mask_every > 0 and step % args.resample_mask_every == 0

                # [B, H*W] 
                noisy_x = torch.where(
                    torch.rand_like(x_mb) > args.input_dropout, x_mb, torch.zeros_like(x_mb)
                )
                p_x = model(
                    inputs=context,
                    history=noisy_x,
                    resample_mask=resample_mask
                )
                # [B, H*W]
                ll_mb = p_x.log_prob(x_mb)
                # [B]
                ll = ll_mb.sum(-1)

                loss = -(ll).mean()
                loss.backward()
                gen_opt.step()    


                display = OrderedDict()
                display['0s'] = '{:.2f}'.format((x_mb == 0).float().mean().item())
                display['1s'] = '{:.2f}'.format((x_mb == 1).float().mean().item())
                display['NLL'] =  '{:.2f}'.format(-ll.mean().item())     

                if writer:            
                    writer.add_scalar('training/LL', ll)
                    #writer.add_image('training/posterior/sample', z.mean(0).reshape(1,1,-1) * 255)

                iterator.set_postfix(display, refresh=False)
                step += 1

            stop, dict_valid = validate(self.get_batcher(self.valid_loader), args, model, gen_opt, gen_scheduler, 
                writer=writer, name="dev")

            if stop:
                print('Early stopping at epoch {:3}/{}'.format(epoch + 1, args.epochs))
                break

            print('Epoch {:3}/{} -- '.format(epoch + 1, args.epochs) + \
                  ', '.join(['{}: {:4.2f}'.format(k, v) for k, v in sorted(dict_valid.items())]))

    def validate(self):
        """Check validation performance (note this will not update the learning rate scheduler"""
        stop, dict_valid = validate(
            self.get_batcher(self.valid_loader), 
            self.args, 
            self.model, 
            name="dev"
        )
        return dict_valid

    def test(self):
        """Check test performance (note this will not update the learning rate scheduler)"""
        stop, dict_test = validate(
            self.get_batcher(self.test_loader), 
            self.args, 
            self.model, 
            name="test"
        )
        return dict_test    
   
    
if __name__ == '__main__':
    Experiment(config()).train()