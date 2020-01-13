import torch
from pathlib import Path
import copy
import time
import torch.nn.functional as F
import matplotlib.pyplot as plt
import numpy as np
import pdb
import skimage
from distutils.version import LooseVersion
from skimage.transform import resize as sk_resize

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

from torchvision.ops import RoIAlign
import os
os.environ['KMP_DUPLICATE_LIB_OK']='True'

roi_align = RoIAlign((28,28), spatial_scale=1.0, sampling_ratio=2)

def get_rois(batch_size):
    rois_np = np.zeros((batch_size, 5))
    for i in range(len(rois_np)):
        rois_np[i] = [i, 0, 0, 233, 233]
    rois = rois_np.astype(np.float32)
    rois = torch.from_numpy(rois)
    return rois

def viz_prediction(track_sample, pred, epoch):
    scans, label = track_sample

    scans, label = scans.numpy().transpose((1, 2, 0)), label.numpy()[0][..., np.newaxis]
    pred = pred[0].numpy()[..., np.newaxis]

    scans_stack = np.concatenate([scans, label, pred], axis=-1)

    fig = plt.figure(figsize=(20, 6))

    fig.suptitle('TRACKING Sample')

    for slice_, scan in enumerate(['dwi', 'flair', 't1', 't2', 'label', 'predicted']):
        ax = plt.subplot(1, 6, slice_ + 1)
        show_single_img(scans_stack[:, :, slice_], (scan == 'label' or scan == 'predicted'))
        plt.tight_layout()
        ax.set_title(scan)
        ax.axis('off')

    # plt.show()
    plt.savefig('sample_tracking/'+ str(epoch)+ '.jpg')


def show_single_img(image, label):
    """Show image"""
    cmap = 'gray'
    if label:
        cmap = 'binary'
    plt.imshow(image, cmap = cmap)


def dice_loss(input, target):
    smooth = 1.

    iflat = input.view(-1)
    tflat = target.view(-1)
    intersection = (iflat * tflat).sum()

    return 1 - ((2. * intersection + smooth) /
                (iflat.sum() + tflat.sum() + smooth))


def resize(image, output_shape, order=1, mode='constant', cval=0, clip=True,
           preserve_range=False, anti_aliasing=False, anti_aliasing_sigma=None):
    """A wrapper for Scikit-Image resize().
    Scikit-Image generates warnings on every call to resize() if it doesn't
    receive the right parameters. The right parameters depend on the version
    of skimage. This solves the problem by using different parameters per
    version. And it provides a central place to control resizing defaults.
    """
    if LooseVersion(skimage.__version__) >= LooseVersion("0.14"):
        # New in 0.14: anti_aliasing. Default it to False for backward
        # compatibility with skimage 0.13.
        return skimage.transform.resize(
            image, output_shape,
            order=order, mode=mode, cval=cval, clip=clip,
            preserve_range=preserve_range, anti_aliasing=anti_aliasing,
            anti_aliasing_sigma=anti_aliasing_sigma)
    else:
        return skimage.transform.resize(
            image, output_shape,
            order=order, mode=mode, cval=cval, clip=clip,
            preserve_range=preserve_range)


def unmold_mask(mask, bbox, image_shape):
    """Converts a mask generated by the neural network to a format similar
    to its original shape.
    mask: [height, width] of type float. A small, typically 28x28 mask.
    bbox: [y1, x1, y2, x2]. The box to fit the mask in.
    Returns a binary mask with the same size as the original image.
    """
    threshold = 0.5
    y1, x1, y2, x2 = bbox
    mask = resize(mask, (y2 - y1, x2 - x1))
    mask = np.where(mask >= threshold, 1, 0).astype(np.bool)

    # Put the mask in the right location.
    full_mask = np.zeros(image_shape, dtype=np.bool)
    full_mask[y1:y2, x1:x2] = mask
    return full_mask

def model_out_to_unmold(outputs28):
    batch_size = outputs28.size(0)
    outputs28_np = outputs28.detach().cpu().numpy()  # has shape (batch_size, 1, 28, 28)
    outputs28_np = outputs28_np[:, 0, :, :].transpose(1, 2, 0)  # makes it (28, 28, batch_size)

    preds224 = unmold_mask(outputs28_np, [0, 0, 223, 223], (224, 224, batch_size))[np.newaxis, ...]\
        .transpose(3, 0, 1, 2)\
        .astype(np.float32)  # outputs (224,224, batch_size) - insert axis at 0, do another transpose

    return torch.from_numpy(preds224)

class_weights = torch.tensor([0.1, 0.9]).to(device)
new_path = 'learning_rate_changed.pth'

def train_model(model, optimizer, scheduler, dataloaders, dataset_sizes, track_sample, batch_size = 32, num_epochs=25):
    since = time.time()
    PATH = 'baseline.pth'
    epo = 1

    if Path(PATH).is_file():
        checkpoint = torch.load(PATH)
        model.load_state_dict(checkpoint['model_state_dict'])
        # optimizer.load_state_dict(checkpoint['optimizer_state_dict'])
        epo = checkpoint['epoch']
        loss = checkpoint['loss']
        scheduler.load_state_dict(checkpoint['scheduler_state_dict'])

        print('Resuming from epoch ' + str(epo)+ ', LOSS: ', loss.item())

    best_model_wts = copy.deepcopy(model.state_dict())
    best_acc = 0.0

    logs_ptr = open('train_logs', 'a')

    # pdb.set_trace()
    for epoch in range(epo, epo+num_epochs):
        epoch_str = 'Epoch {}/{}'.format(epoch, epo + num_epochs-1) +'\n\n'
        print(epoch_str)
        logs_ptr.write(epoch_str)

        print('-' * 10)

        try:
            # Each epoch has a training and validation phase
            for phase in ['train', 'val']:
                if phase == 'train':
                    model.train()  # Set model to training mode
                else:
                    model.eval()   # Set model to evaluate mode

                running_loss = 0.0
                running_corrects = 0
                running_dice = 0.0

                # Iterate over data.
                times = 0

                for mini_batch, (inputs, label224, label28) in enumerate(dataloaders[phase]):

                    inputs = inputs.to(device)

                    # labels size is (batch_size, 1, 224, 224)
                    mask28 = label28.to(device)

                    # zero the parameter gradients
                    optimizer.zero_grad()

                    # forward
                    # track history if only in train
                    with torch.set_grad_enabled(phase == 'train'):
                        outputs28 = model(inputs)

                        loss = F.nll_loss(outputs28, mask28, weight=class_weights)

                        _, outputs28 = torch.max(torch.exp(outputs28), dim=1, keepdim=True)

                        outputs28, mask28 = outputs28.float(), mask28.float()

                        dice_l = dice_loss(input=outputs28, target=mask28)

                        total_loss = dice_l + loss
                        # loss = criterion(outputs, labels)

                        # backward + optimize only if in training phase
                        if phase == 'train':
                            total_loss.backward()
                            optimizer.step()

                    # step_loss = loss
                    step_dice_l = dice_l
                    step_loss = loss

                    torch_preds224 = model_out_to_unmold(outputs28)   # gives back (batch_size, 1, 224, 224)

                    step_corrects = torch.sum(torch_preds224 == label224.data)

                    step_acc = step_corrects.double().item() / label224.data.view(-1).size(0)

                    # dice = dice_loss(input= labels, target=preds)

                    if phase == 'train':
                        step_str = '{} Step: {} SLoss: {:.4f} Dice Loss: {:.4f} Acc: {:.4f} %'.format(
                            phase, mini_batch+1, step_loss, step_dice_l, step_acc * 100.0) + '\n'
                        print(step_str)

                        logs_ptr.write(step_str)

                    # statistics
                    # running_loss += step_loss.item() * inputs.size(0)
                    running_corrects += step_corrects  # done for batch size inputs.
                    running_dice += step_dice_l.item() * inputs.size(0)
                    running_loss += step_loss.item() * inputs.size(0)

                # end of an epoch
                # pdb.set_trace()

                # epoch_loss = running_loss / dataset_sizes[phase]
                epoch_acc = running_corrects.double() / (dataset_sizes[phase] * 224 * 224)
                epoch_dice_l = running_dice / dataset_sizes[phase]
                epoch_loss = running_loss / dataset_sizes[phase]

                if phase == 'train':
                    track_scans, track_labels= track_sample
                    track_scans = track_scans.unsqueeze(0).to(device)

                    pred28 = model(track_scans)  # shape of pred28 is (batch_size, 2, 28, 28)
                    _, pred28 = torch.max(torch.exp(pred28), dim=1, keepdim=True)

                    pred224 = model_out_to_unmold(pred28) # (batch_size, 1, 28, 28)

                    viz_prediction(track_sample, pred224[0], epoch)
                    scheduler.step()

                loss_str= '\n{} Epoch {}: SLoss: {:.4f} Dice Loss: {:.4f} Acc: {:.4f} %\n'.format(
                    phase, epoch, epoch_loss, epoch_dice_l, epoch_acc * 100.0) + '\n'
                print(loss_str)

                logs_ptr.write(loss_str+ '\n')

                # deep copy the model
                if phase == 'val' and epoch_acc > best_acc:
                    print('Val acc better than Best acc')
                    best_acc = epoch_acc
                    best_model_wts = copy.deepcopy(model.state_dict())

        except:
            # save model
            save_model(epoch, best_model_wts, optimizer, scheduler, loss, new_path)
            exit(0)

        print()

    time_elapsed = time.time() - since
    print('Training complete in {:.0f}m {:.0f}s'.format(
        time_elapsed // 60, time_elapsed % 60))
    print('Best val Acc: {:4f}'.format(best_acc))

    # save model
    save_model(num_epochs,
               best_model_wts,
               optimizer,
               scheduler, loss, new_path)


def save_model(epoch, best_model_wts, optimizer, scheduler, loss, PATH):
    print('Saving model @ epoch = ', epoch)
    torch.save({
        'epoch': epoch,
        'model_state_dict': best_model_wts,
        'optimizer_state_dict': optimizer.state_dict(),
        'scheduler_state_dict': scheduler.state_dict(),
        'loss': loss,
    }, PATH)


# borrow functions and modify it from https://github.com/Kaixhin/FCN-semantic-segmentation/blob/master/main.py
# Calculates class intersections over unions
def iou(pred, target):
    ious = []
    n_class = 2
    for cls in range(n_class):
        pred_inds = pred == cls
        target_inds = target == cls
        intersection = pred_inds[target_inds].sum()
        union = pred_inds.sum() + target_inds.sum() - intersection
        if union == 0:
            ious.append(float('nan'))  # if there is no ground truth, do not include in evaluation
        else:
            ious.append(float(intersection) / max(union, 1))
        # print("cls", cls, pred_inds.sum(), target_inds.sum(), intersection, float(intersection) / max(union, 1))
    return ious


def pixel_acc(pred, target):
    correct = (pred == target).sum()
    total   = (target == target).sum()
    return correct / total


