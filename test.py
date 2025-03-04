import numpy as np
import os
import time
import torch
from torch import nn
import mrcfile
import pytorch_lightning as pl
from torch.utils.data import DataLoader
import sys
# from dataset.dataloader import Dataset_ClsBased
import pandas as pd
import importlib
from glob import glob
import matplotlib.pyplot as plt
from pytorch_lightning import Trainer
import datetime 
from options.option import BaseOptions
from model_.model_loader import get_model
from utils.misc import combine, get_centroids, de_dup, cal_metrics_NMS_OneCls
import tqdm
import json
from dataset.dataloader_DynamicLoad import Dataset_ClsBased


def test_func(args, stdout=None):
    if stdout is not None:
        save_stdout = sys.stdout
        save_stderr = sys.stderr
        sys.stdout = stdout
        sys.stderr = stdout
    for test_idx in args.test_idxs:
        model_name = args.checkpoints.split('/')[-4] + '_' + args.checkpoints.split('/')[-1].split('-')[0]
        # load config parameters
        if len(args.configs) > 0:
            with open(args.configs, 'r') as f:
                cfg = json.loads(''.join(f.readlines()).lstrip('train_configs='))

        start_time = time.time()
        args.data_split[-2] = test_idx
        args.data_split[-1] = test_idx + 1

        num_name = pd.read_csv(os.path.join(cfg["tomo_path"], 'num_name.csv'), sep='\t', header=None)
        dir_list = num_name.iloc[:, 1]
        dir_name = dir_list[args.data_split[-2]]
        print(dir_name)

        tomo_file = glob(cfg["tomo_path"] + "/*%s" % cfg["tomo_format"])[0]
        data_file = mrcfile.open(tomo_file, permissive=True)
        data_shape = data_file.data.shape
        print(data_shape)
        dataset = cfg["dset_name"]

        if args.use_seg:
            for pad_size in args.pad_size:

                class UNetTest(pl.LightningModule):
                    def __init__(self):
                        super(UNetTest, self).__init__()
                        self.model = get_model(args)
                        #self.partical_volume = 4 / 3 * np.pi * (cfg["label_diameter"] / 2) ** 3
                        self.num_classes = args.num_classes

                    def forward(self, x):
                        return self.model(x)

                    def test_step(self, test_batch, batch_idx):
                        with torch.no_grad():
                            img, label, index = test_batch
                            index = torch.cat([i.view(1, -1) for i in index], dim=0).permute(1, 0)
                            if args.use_paf:
                                seg_output, paf_output, logsigma1 = self.forward(img)
                            else:
                                seg_output = self.forward(img)
                            if args.test_use_pad:
                                mp_num = int(sorted([int(i) for i in cfg["ocp_diameter"].split(',')])[-1] / (args.meanPool_kernel - 1) + 1)
                                if args.num_classes > 1:
                                    out = self._nms_v2(seg_output[:, 1:], kernel=args.meanPool_kernel,
                                                        mp_num=mp_num, positions=index), (index, seg_output)
                                else:
                                    out = self._nms_v2(seg_output[:, :], kernel=args.meanPool_kernel,
                                                        mp_num=mp_num, positions=index), (index, seg_output)
                            return out
                        
                    def test_step_end(self, outputs):
                        return outputs

                    def test_epoch_end(self, epoch_output):
                        out_dir = '/'.join(args.checkpoints.split('/')[:-2]) + f'/{args.out_name}'
                        
                        index = torch.cat([i[1][0] for i in epoch_output], dim=0)
                        seg_output = torch.cat([i[1][1] for i in epoch_output], dim=0)
                        # version_X directory 
                        versino_dir = '/'.join(out_dir.split('/')[:-1])
                        out_dir_tomo = f"{versino_dir}/full_segmentation_output"
                        os.makedirs(out_dir_tomo, exist_ok=True)
                        full_tomogram = self._reassemble(seg_output, index)
                        torch.save(full_tomogram, os.path.join(out_dir_tomo, f'{dir_name}.pt'))
                        print(f"Saved full tomogram to {os.path.join(out_dir_tomo, f'{dir_name}.pt')}")
                        
                        
                        #torch.save(index, os.path.join(out_dir, 'index.pt'))
                        #torch.save(seg_output, os.path.join(out_dir, 'seg_output.pt'))                        
                        
                        
                        with torch.no_grad():
                            if args.meanPool_NMS:
                                epoch_output = [e[0] for e in epoch_output]
                                coords_out = torch.cat(epoch_output, dim=0).detach().cpu().numpy()
                                print('coords_out:', coords_out.shape)
                                if args.de_duplication:
                                    centroids = de_dup(coords_out, args)
                                else:
                                    centroids = coords_out
                                os.makedirs(os.path.join(out_dir, 'Coords_withArea'), exist_ok=True)
                                np.savetxt(os.path.join(out_dir, 'Coords_withArea', dir_name + '.coords'),
                                           centroids.astype(float),
                                           fmt='%s',
                                           delimiter='\t')

                                coords = centroids[:, 0:4]
                                os.makedirs(os.path.join(out_dir, 'Coords_All'), exist_ok=True)
                                np.savetxt(os.path.join(out_dir, 'Coords_All', dir_name + '.coords'),
                                           coords.astype(int),
                                           fmt='%s',
                                           delimiter='\t')


                    def test_dataloader(self):
                        if args.test_mode == 'test':
                            test_dataset = Dataset_ClsBased(mode='test',
                                                            block_size=args.block_size,
                                                            num_class=args.num_classes,
                                                            random_num=args.random_num,
                                                            use_bg=args.use_bg,
                                                            data_split=args.data_split,
                                                            test_use_pad=args.test_use_pad,
                                                            pad_size=pad_size,
                                                            cfg=cfg,
                                                            args=args)
                            test_dataloader = DataLoader(test_dataset,
                                                         shuffle=False,
                                                         batch_size=args.batch_size,
                                                         num_workers=8 if args.batch_size >= 32 else 4,
                                                         pin_memory=False)

                            self.len_block = test_dataset.test_len
                            self.data_shape = test_dataset.data_shape
                            self.occupancy_map = test_dataset.occupancy_map
                            self.gt_coords = test_dataset.gt_coords
                            self.dir_name = test_dataset.dir_name
                            return test_dataloader
                        elif args.test_mode == 'test_only':
                            test_dataset = Dataset_ClsBased(mode='test_only',
                                                            block_size=args.block_size,
                                                            num_class=args.num_classes,
                                                            random_num=args.random_num,
                                                            use_bg=args.use_bg,
                                                            data_split=args.data_split,
                                                            test_use_pad=args.test_use_pad,
                                                            pad_size=pad_size,
                                                            cfg=cfg,
                                                            args=args)
                            if args.batch_size <= 32:
                                num_work = 4
                            elif args.batch_size <= 64:
                                num_work = 8
                            elif args.batch_size <= 128:
                                num_work = 8
                            else:
                                num_work = 16
                            test_dataloader = DataLoader(test_dataset,
                                                         shuffle=False,
                                                         batch_size=args.batch_size,
                                                         num_workers=num_work,
                                                         pin_memory=False)
                            self.len_block = test_dataset.test_len
                            self.data_shape = test_dataset.data_shape
                            self.dir_name = test_dataset.dir_name
                            return test_dataloader

                    def _nms_v2(self, pred, kernel=3, mp_num=5, positions=None):
                        with torch.no_grad():
                            pred = torch.where(pred > 0.5, 1, 0)
                            meanPool = nn.AvgPool3d(kernel, 1, kernel // 2).to(self.device)
                            maxPool = nn.MaxPool3d(kernel, 1, kernel // 2).to(self.device)
                            hmax = pred.clone().float()
                            for _ in range(mp_num):
                                hmax = meanPool(hmax)    
                            pred = hmax.clone()
                            hmax = maxPool(hmax)
                            
                            keep = ((hmax == pred).float()) * ((pred > 0.1).float())
                            coords = keep.nonzero()  # [N, 5]
                            coords = coords[coords[:, 2] >= args.pad_size[0]]
                            coords = coords[coords[:, 2] <= args.block_size - args.pad_size[0]]
                            coords = coords[coords[:, 3] >= args.pad_size[0]]
                            coords = coords[coords[:, 3] <= args.block_size - args.pad_size[0]]
                            coords = coords[coords[:, 4] >= args.pad_size[0]]
                            coords = coords[coords[:, 4] <= args.block_size - args.pad_size[0]]

                            h_val = hmax[coords[:, 0], coords[:, 1], coords[:, 2], coords[:, 3], coords[:, 4]].unsqueeze(1)
                            # below is the orignal, slow version of h_val
                            #h_val_ = torch.cat([hmax[item[0], item[1], item[2], item[3]:item[3] + 1, item[4]:item[4] + 1] for item in coords], dim=0)
                            #assert torch.all(h_val == h_val_)
                            
                            try:
                                leftTop_coords = positions[coords[:, 0]] - (args.block_size // 2) - args.pad_size[0]
                                coords[:, 2:5] = coords[:, 2:5] + leftTop_coords


                                pred_final = torch.cat([coords[:, 1:2] + 1, coords[:, 4:5], coords[:, 3:4], coords[:, 2:3], h_val], dim=1)
                                return pred_final
                            except:
                                print('haha')
                                return torch.zeros([0, 5]).cuda()
                
                    def _reassemble(self, seg_output, index):
                        block_size = args.block_size
                        pad_size = args.pad_size[0]
                        """
                        Modification! Reassemble the sub-tomograms to full tomogram
                        """
                        seg_output = seg_output.cpu()
                        seg_output_crop = torch.stack([seg_output[..., pad_size:-pad_size, pad_size:-pad_size, pad_size:-pad_size] for seg_output in seg_output.squeeze()])
                        index = index.cpu()

                        top_left = index - (block_size // 2) - pad_size
                        full_z, full_y, full_x = torch.max(top_left + block_size, dim=0).values
                        
                        if len(seg_output_crop.shape) == 5:
                            num_classes = seg_output_crop.shape[1]
                            # Initialize full tomogram and count matrix
                            full_tomogram = torch.zeros((num_classes, full_z, full_y, full_x), device=seg_output.device)
                            count_matrix = torch.zeros((num_classes, full_z, full_y, full_x), device=seg_output.device)
                        else:
                            full_tomogram = torch.zeros((full_z, full_y, full_x), device=seg_output.device)
                            count_matrix = torch.zeros((full_z, full_y, full_x), device=seg_output.device)


                        # Load your sub-tomograms
                        for i, (z, y, x) in tqdm.tqdm(enumerate(top_left), total=len(top_left), desc="Building full tomogram"):
                            
                            z_start = z + pad_size
                            y_start = y + pad_size
                            x_start = x + pad_size
                            
                            z_end = z_start + block_size - 2*pad_size
                            y_end = y_start + block_size - 2*pad_size
                            x_end = x_start + block_size - 2*pad_size
                            
                            # insert sub-tomogram data
                            seg_output_crop = seg_output.squeeze()[i, ..., pad_size:-pad_size, pad_size:-pad_size, pad_size:-pad_size]
                            full_tomogram[..., z_start:z_end, y_start:y_end, x_start:x_end] += seg_output_crop
                            count_matrix[..., z_start:z_end, y_start:y_end, x_start:x_end] += 1

                        # average overlapping regions
                        count_matrix[count_matrix == 0] = 1  # Prevent division by zero
                        full_tomogram /= count_matrix
                        return full_tomogram

                # load trained checkpoints to model
                # try:
                #     model = UNetTest.load_from_checkpoint(args.checkpoints)
                # except:
                #  print('Loading model from checkpoint failed. Trying to load model alternatively.')
                model = UNetTest()
                state_dict = torch.load(args.checkpoints)['state_dict']
                state_dict_ = {k: v for k, v in zip(model.model.state_dict().keys(), state_dict.values())}
                #state_dict = {k: v.cpu() for k, v in state_dict.items()}
                #state_dict_ = {k: v for k, v in zip(model.state_dict().keys(), state_dict.values())}
                model.model.load_state_dict(state_dict_)
                # model = UNetTest().model
                model.eval()
                runner = Trainer(gpus=args.gpu_id, #
                                 accelerator='dp'
                                 )
                os.makedirs(f'result/{dataset}/{model_name}/', exist_ok=True)

                runner.test(model=model)
                
                    

        end_time = time.time()
        used_time = end_time - start_time
        save_path = '/'.join(args.checkpoints.split('/')[:-2]) + f'/{args.out_name}'
        os.makedirs(save_path, exist_ok=True)
        pad_size = args.pad_size[0]
        
        with torch.no_grad():
            torch.cuda.empty_cache()

    print('*' * 100)
    print('Testing Finished!')
    print('*' * 100)
    if stdout is not None:
        sys.stdout = save_stdout
        sys.stderr = save_stderr

