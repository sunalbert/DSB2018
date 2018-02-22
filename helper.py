import os
import json
import numpy as np
import torch
from scipy import ndimage as ndi
from skimage.morphology import label, watershed, remove_small_objects
from skimage.feature import peak_local_max
from skimage.measure import regionprops
import configparser

# config related handling
def run_once(func):
    ''' a declare wrapper function to call only once, use @run_once declare keyword '''
    def wrapper(*args, **kwargs):
        if 'result' not in wrapper.__dict__:
            wrapper.result = func(*args, **kwargs)
        return wrapper.result
    return wrapper

@run_once
def read_config():
    conf = configparser.ConfigParser()
    candidates = ['config_default.ini', 'config.ini']
    conf.read(candidates)
    return conf

config = read_config() # keep the line as top as possible

# copy from https://github.com/pytorch/examples/blob/master/imagenet/main.py#L139
class AverageMeter():
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

# copy from https://www.kaggle.com/aglotero/another-iou-metric
def iou_metric(y_pred_in, y_true_in, instance_level=False, print_table=False):
    threshold = config['param'].getfloat('threshold')

    y_pred = label(y_pred_in > threshold)
    if instance_level:
        labels = y_true_in
    else:
        labels = label(y_true_in > threshold)

    true_objects = len(np.unique(labels))
    pred_objects = len(np.unique(y_pred))

    intersection = np.histogram2d(labels.flatten(), y_pred.flatten(), bins=(true_objects, pred_objects))[0]

    # Compute areas (needed for finding the union between all objects)
    area_true = np.histogram(labels, bins = true_objects)[0]
    area_pred = np.histogram(y_pred, bins = pred_objects)[0]
    area_true = np.expand_dims(area_true, -1)
    area_pred = np.expand_dims(area_pred, 0)

    # Compute union
    union = area_true + area_pred - intersection

    # Exclude background from the analysis
    intersection = intersection[1:,1:]
    union = union[1:,1:]
    union[union == 0] = 1e-9

    # Compute the intersection over union
    iou = intersection / union

    # Precision helper function
    def precision_at(threshold, iou):
        matches = iou > threshold
        true_positives = np.sum(matches, axis=1) == 1   # Correct objects
        false_positives = np.sum(matches, axis=0) == 0  # Missed objects
        false_negatives = np.sum(matches, axis=1) == 0  # Extra objects
        tp, fp, fn = np.sum(true_positives), np.sum(false_positives), np.sum(false_negatives)
        return tp, fp, fn

    # Loop over IoU thresholds
    prec = []
    if print_table:
        print("Thresh\tTP\tFP\tFN\tPrec.")
    for t in np.arange(0.5, 1.0, 0.05):
        tp, fp, fn = precision_at(t, iou)
        if (tp + fp + fn) > 0:
            p = tp / (tp + fp + fn)
        else:
            p = 0
        if print_table:
            print("{:1.3f}\t{}\t{}\t{}\t{:1.3f}".format(t, tp, fp, fn, p))
        prec.append(p)

    if print_table:
        print("AP\t-\t-\t-\t{:1.3f}".format(np.mean(prec)))
    return np.mean(prec)

def iou_mean(y_pred_in, y_true_in, instance_level=False):
    y_pred_in = y_pred_in.data.cpu().numpy()
    y_true_in = y_true_in.data.cpu().numpy()
    batch_size = y_true_in.shape[0]
    metric = []
    for batch in range(batch_size):
        value = iou_metric(y_pred_in[batch], y_true_in[batch], instance_level=instance_level)
        metric.append(value)
    return np.mean(metric)

# Run-length encoding stolen from https://www.kaggle.com/rakhlin/fast-run-length-encoding-python
def rle_encoding(y):
    dots = np.where(y.T.flatten() == 1)[0]
    run_lengths = []
    prev = -2
    for b in dots:
        if (b>prev+1): run_lengths.extend((b + 1, 0))
        run_lengths[-1] += 1
        prev = b
    return run_lengths

def prob_to_rles(y):
    threshold = config['param'].getfloat('threshold')
    segmentation = config['post'].getboolean('segmentation')
    remove_objects = config['post'].getboolean('remove_objects')
    min_object_size = config['post'].getint('min_object_size')

    y = y > threshold
    if remove_objects:
        y = remove_small_objects(y, min_size=min_object_size)
    lab_img = label(y)
    if segmentation:
        lab_img = seg_ws(lab_img)
    for i in range(1, lab_img.max() + 1):
        yield rle_encoding(lab_img == i)

# checkpoint handling
def ckpt_path(epoch=None):
    checkpoint_dir = os.path.join('.', 'checkpoint')
    current_path = os.path.join('.', 'checkpoint', 'current.json')
    if not os.path.exists(checkpoint_dir):
        os.makedirs(checkpoint_dir)
    if epoch is None:
        if os.path.exists(current_path):
            with open(current_path) as infile:
                data = json.load(infile)
                epoch = data['epoch']
        else:
            return ''
    else:
        with open(current_path, 'w') as outfile:
            json.dump({
                'epoch': epoch
            }, outfile)
    return os.path.join(checkpoint_dir, 'ckpt-{}.pkl'.format(epoch))

def save_ckpt(model, optimizer, epoch):
    ckpt = ckpt_path(epoch)
    torch.save({
        'epoch': epoch,
        'model': model.state_dict(),
        'optimizer': optimizer.state_dict(),
    }, ckpt)

def load_ckpt(model, optimizer=None):
    ckpt = ckpt_path()
    epoch = 0
    if os.path.isfile(ckpt):
        print("Loading checkpoint '{}'".format(ckpt))
        if torch.cuda.is_available():
            # Load all tensors onto previous state
            checkpoint = torch.load(ckpt)
        else:
            # Load all tensors onto the CPU
            checkpoint = torch.load(ckpt, map_location=lambda storage, loc: storage)
        epoch = checkpoint['epoch']
        model.load_state_dict(checkpoint['model'])
        if optimizer:
            optimizer.load_state_dict(checkpoint['optimizer'])
    return epoch

# Evaluate the average nucleus size.
def evaluate_size(image, ratio):
    label_image = label(image)
    label_counts = len(np.unique(label_image))
    #Sort Area sizes:
    areas = [r.area for r in regionprops(label_image)]
    areas.sort()
    total_area = 0
    #To avoild eval_count ==0
    if int(label_counts * ratio)==0:
        eval_count = 1
    else:
        eval_count = int(label_counts * ratio)
    average_area = np.array(areas[:eval_count]).mean()
    size_index = average_area ** 0.5
    return size_index

# Segment image with watershed algorithm.
def seg_ws(image):
    size_scale=config['post'].getfloat('seg_scale')
    ratio=config['post'].getfloat('seg_ratio')

    #Calculate the average size of the image.
    size_index = evaluate_size(image, ratio)
    """
    Add noise to fix min_distance bug:
    If multiple peaks in the specified region have identical intensities,
    the coordinates of all such pixels are returned.
    """
    noise = np.random.randn(image.shape[0], image.shape[1]) * 0.1
    distance = ndi.distance_transform_edt(image)+noise
    #2*min_distance+1 is the minimum distance between two peaks.
    local_maxi = peak_local_max(distance, min_distance=(size_index*size_scale), exclude_border=False, indices=False,
                                labels=image)
    markers = ndi.label(local_maxi)[0]
    labels = watershed(-distance, markers, mask=image)
    return labels

def seg_ws_by_edge(raw_bodies, raw_edges):
    threshold=config['param'].getfloat('threshold')
    k=config['post'].getfloat('edge_weight_factor')

    bodies = raw_bodies > threshold
    # edges = raw_edges > threshold
    seeds = ((raw_bodies - k * raw_edges) > threshold)
    # seeds = bodies & ~edges
    labels = label(seeds)
    final_labels = watershed(-ndi.distance_transform_edt(bodies), labels, mask=bodies)
    return final_labels