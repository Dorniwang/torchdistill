import argparse
import datetime
import time

import torch
from torch import distributed as dist
from torch.backends import cudnn
from torch.nn import DataParallel
from torch.nn.parallel import DistributedDataParallel

from kdkit.common import main_util
from kdkit.common.constant import def_logger
from kdkit.common.main_util import load_ckpt, save_ckpt
from kdkit.datasets import util
from kdkit.eval.classification import compute_accuracy
from kdkit.misc.log import setup_log_file, SmoothedValue, MetricLogger
from kdkit.models import MODEL_DICT
from kdkit.models.official import get_image_classification_model
from kdkit.tools.distillation import get_distillation_box
from myutils.common import file_util, yaml_util
from myutils.pytorch import module_util

logger = def_logger.getChild(__name__)


def get_argparser():
    parser = argparse.ArgumentParser(description='Knowledge distillation for image classification models')
    parser.add_argument('--config', required=True, help='yaml file path')
    parser.add_argument('--device', default='cuda', help='device')
    parser.add_argument('--log', help='log file path')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N', help='start epoch')
    parser.add_argument('-sync_bn', action='store_true', help='Use sync batch norm')
    parser.add_argument('-test_only', action='store_true', help='Only test the models')
    parser.add_argument('-student_only', action='store_true', help='Test the student model only')
    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int, help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    return parser


def get_model(model_config, device, distributed, sync_bn):
    model = get_image_classification_model(model_config, distributed, sync_bn)
    if model is None:
        model = MODEL_DICT[model_config['name']](**model_config['params'])

    ckpt_file_path = model_config['ckpt']
    load_ckpt(ckpt_file_path, model=model, strict=True)
    return model.to(device)


def distill_one_epoch(distillation_box, device, epoch, log_freq):
    metric_logger = MetricLogger(delimiter='  ')
    metric_logger.add_meter('lr', SmoothedValue(window_size=1, fmt='{value}'))
    metric_logger.add_meter('img/s', SmoothedValue(window_size=10, fmt='{value}'))
    header = 'Epoch: [{}]'.format(epoch)
    for sample_batch, targets, supp_dict in \
            metric_logger.log_every(distillation_box.train_data_loader, log_freq, header):
        start_time = time.time()
        sample_batch, targets = sample_batch.to(device), targets.to(device)
        loss = distillation_box(sample_batch, targets, supp_dict)
        distillation_box.update_params(loss)
        batch_size = sample_batch.shape[0]
        metric_logger.update(loss=loss.item(), lr=distillation_box.optimizer.param_groups[0]['lr'])
        metric_logger.meters['img/s'].update(batch_size / (time.time() - start_time))


@torch.no_grad()
def evaluate(model, data_loader, device, device_ids, distributed, log_freq=1000, title=None, header='Test:'):
    model.to(device)
    if distributed:
        model = DistributedDataParallel(model, device_ids=device_ids)
    elif device.type.startswith('cuda'):
        model = DataParallel(model, device_ids=device_ids)

    if title is not None:
        logger.info(title)

    num_threads = torch.get_num_threads()
    torch.set_num_threads(1)
    model.eval()
    metric_logger = MetricLogger(delimiter='  ')
    for image, target in metric_logger.log_every(data_loader, log_freq, header):
        image = image.to(device, non_blocking=True)
        target = target.to(device, non_blocking=True)
        output = model(image)
        acc1, acc5 = compute_accuracy(output, target, topk=(1, 5))
        # FIXME need to take into account that the datasets
        # could have been padded in distributed setup
        batch_size = image.shape[0]
        metric_logger.meters['acc1'].update(acc1.item(), n=batch_size)
        metric_logger.meters['acc5'].update(acc5.item(), n=batch_size)

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    top1_accuracy = metric_logger.acc1.global_avg
    top5_accuracy = metric_logger.acc5.global_avg
    logger.info(' * Acc@1 {:.4f}\tAcc@5 {:.4f}\n'.format(top1_accuracy, top5_accuracy))
    torch.set_num_threads(num_threads)
    return metric_logger.acc1.global_avg


def distill(teacher_model, student_model, dataset_dict, device, device_ids, distributed, start_epoch, config, args):
    logger.info('Start knowledge distillation')
    train_config = config['train']
    distillation_box =\
        get_distillation_box(teacher_model, student_model, dataset_dict, train_config, device, device_ids, distributed)
    ckpt_file_path = config['models']['student_model']['ckpt']
    best_val_top1_accuracy = 0.0
    optimizer, lr_scheduler = distillation_box.optimizer, distillation_box.lr_scheduler
    if file_util.check_if_exists(ckpt_file_path):
        best_val_top1_accuracy, _, _ = load_ckpt(ckpt_file_path, optimizer=optimizer, lr_scheduler=lr_scheduler)

    log_freq = train_config['log_freq']
    student_model_without_ddp = student_model.module if module_util.check_if_wrapped(student_model) else student_model
    start_time = time.time()
    for epoch in range(start_epoch, distillation_box.num_epochs):
        distillation_box.pre_process(epoch=epoch)
        distill_one_epoch(distillation_box, device, epoch, log_freq)
        val_top1_accuracy = evaluate(student_model, distillation_box.val_data_loader, device, device_ids, distributed,
                                     log_freq=log_freq, header='Validation:')
        if val_top1_accuracy > best_val_top1_accuracy and main_util.is_main_process():
            logger.info('Updating ckpt (Best top1 accuracy: '
                        '{:.4f} -> {:.4f})'.format(best_val_top1_accuracy, val_top1_accuracy))
            best_val_top1_accuracy = val_top1_accuracy
            save_ckpt(student_model_without_ddp, optimizer, lr_scheduler,
                      best_val_top1_accuracy, config, args, ckpt_file_path)
        distillation_box.post_process()

    if distributed:
        dist.barrier()

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    logger.info('Training time {}'.format(total_time_str))
    distillation_box.clean_modules()


def main(args):
    log_file_path = args.log
    if main_util.is_main_process() and log_file_path is not None:
        setup_log_file(log_file_path)

    distributed, device_ids = main_util.init_distributed_mode(args.world_size, args.dist_url)
    logger.info(args)
    cudnn.benchmark = True
    config = yaml_util.load_yaml_file(args.config)
    device = torch.device(args.device)
    dataset_dict = util.get_all_dataset(config['datasets'])
    models_config = config['models']
    teacher_model_config = models_config['teacher_model']
    teacher_model = get_model(teacher_model_config, device, distributed, False)
    student_model_config = models_config['student_model']
    student_model = get_model(student_model_config, device, distributed, args.sync_bn)
    start_epoch = args.start_epoch
    if not args.test_only:
        distill(teacher_model, student_model, dataset_dict, device, device_ids, distributed, start_epoch, config, args)
        student_model_without_ddp =\
            student_model.module if module_util.check_if_wrapped(student_model) else student_model
        load_ckpt(student_model_config['ckpt'], model=student_model_without_ddp, strict=True)

    test_config = config['test']
    test_data_loader_config = test_config['test_data_loader']
    test_data_loader = util.build_data_loader(dataset_dict[test_data_loader_config['dataset_id']],
                                              test_data_loader_config, distributed)
    if not args.student_only:
        evaluate(teacher_model, test_data_loader, device, device_ids, distributed,
                 title='[Teacher: {}]'.format(teacher_model_config['name']))
    evaluate(student_model, test_data_loader, device, device_ids, distributed,
             title='[Student: {}]'.format(student_model_config['name']))


if __name__ == '__main__':
    argparser = get_argparser()
    main(argparser.parse_args())