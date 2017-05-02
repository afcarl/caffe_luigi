import cremi
import h5py
import json
import numpy as np
from scipy.ndimage import binary_erosion
import os
import time
import waterz
from agglomerate import agglomerate
from roi import Roi

# in nm, equivalent to CREMI metric
neuron_ids_border_threshold = 25

def crop(a, bb):

    cur_shape = list(a.shape[-3:])
    print("Cropping from " + str(cur_shape) + " to " + str(bb))
    if len(a.shape) == 3:
        a = a[bb]
    elif len(a.shape) == 4:
        a = a[(slice(0,4),)+bb]
    else:
        raise RuntimeError("encountered array of dimension " + str(len(a.shape)))

    return a

def get_gt_roi(gt):

    # no-label ids are <0, i.e. the highest numbers in uint64
    fg_indices = np.where(gt <= np.uint64(-10))
    return Roi(
            (np.min(fg_indices[d]) for d in range(3)),
            (np.max(fg_indices[d]) - np.min(fg_indices[d]) for d in range(3))
    )

def evaluate(
        setup,
        iteration,
        sample,
        thresholds,
        output_basenames,
        custom_fragments,
        histogram_quantiles,
        discrete_queue,
        merge_function = None,
        dilate_mask = 0,
        mask_fragments = False,
        keep_segmentation = False):

    if isinstance(setup, int):
        setup = 'setup%02d'%setup

    thresholds = list(thresholds)

    aff_data_dir = os.path.join(os.getcwd(), 'processed', setup, str(iteration))
    affs_filename = os.path.join(aff_data_dir, sample + ".hdf")

    print "Evaluating " + sample + " with " + setup + ", iteration " + str(iteration) + " at thresholds " + str(thresholds)

    print "Reading ground-truth..."
    gt_filename = os.path.join('../01_data', sample + '.hdf')
    if 'resolution' not in h5py.File(gt_filename, 'r')['volumes/labels/neuron_ids'].attrs:
        print("WARNING: file " + gt_filename + " does not contain resolution attribute (I add it)")
        h5py.File(gt_filename, 'r+')['volumes/labels/neuron_ids'].attrs['resolution'] = (40,4,4)
    truth = cremi.io.CremiFile(gt_filename, 'r')
    gt_volume = truth.read_neuron_ids()

    print "Getting ground-truth bounding box..."
    gt_roi = get_gt_roi(gt_volume.data)
    print "GT ROI: " + str(gt_roi)

    print "Reading affinities..."
    affs_file = h5py.File(affs_filename, 'r')
    affs = affs_file['volumes/predicted_affs']
    affs_roi = Roi(
            affs_file['volumes/predicted_affs'].attrs['offset'],
            affs.shape[1:]
    )
    print "affs ROI: " + str(affs_roi)

    assert affs_roi.contains(gt_roi), "Predicted affinities do not contain GT region"

    common_roi = gt_roi.intersect(affs_roi)
    common_roi_in_gt   = common_roi
    common_roi_in_affs = common_roi - affs_roi.get_offset()

    print "Common ROI of GT and affs is " + str(common_roi)
    print "Common ROI in GT: " + str(common_roi_in_gt)
    print "Common ROI in affs: " + str(common_roi_in_affs)

    print "Cropping ground-truth to common ROI"
    print "Previous shape: " + str(gt_volume.data.shape)
    gt_volume.data = gt_volume.data[common_roi_in_gt.get_bounding_box()]
    print "New shape: " + str(gt_volume.data.shape)

    print "Cropping affinities to common ROI"
    affs = np.array(affs[(slice(None),) + common_roi_in_affs.get_bounding_box()])
    affs_file.close()

    print "Growing ground-truth boundary..."
    no_gt = gt_volume.data>=np.uint64(-10)
    gt_volume.data[no_gt] = -1

    print no_gt

    assert affs.shape[1:] == gt_volume.data.shape
    assert no_gt.shape == gt_volume.data.shape

    print("GT min/max: " + str(gt_volume.data.min()) + "/" + str(gt_volume.data.max()))
    evaluate = cremi.evaluation.NeuronIds(gt_volume, border_threshold=neuron_ids_border_threshold)
    gt_with_borders = np.array(evaluate.gt, dtype=np.uint32)
    print("GT with border min/max: " + str(gt_with_borders.min()) + "/" + str(gt_with_borders.max()))

    if dilate_mask != 0:
        print "Dilating GT mask..."
        # in fact, we erode the no-GT mask
        no_gt = binary_erosion(no_gt, iterations=dilate_mask, border_value=True)

    assert no_gt.shape == gt_volume.data.shape

    print "Masking affinities outside ground-truth..."
    for d in range(3):
        affs[d][no_gt] = 0

    start = time.time()

    fragments_mask = None
    if mask_fragments:
        fragments_mask = no_gt==False

    i = 0
    for seg_metric in agglomerate(
            affs,
            gt_with_borders,
            thresholds,
            custom_fragments=custom_fragments,
            histogram_quantiles=histogram_quantiles,
            discrete_queue=discrete_queue,
            merge_function=merge_function,
            fragments_mask=fragments_mask):

        output_basename = output_basenames[i]

        if keep_segmentation:

            print "Storing segmentation..."
            f = h5py.File(output_basename + '.hdf', 'w')
            seg = seg_metric[0]
            ds = f.create_dataset('volumes/labels/neuron_ids', seg.shape, compression="gzip", dtype=np.uint64)
            ds[:] = seg
            ds.attrs['offset'] = common_roi.get_offset()
            ds.attrs['resolution'] = (40.0,4.0,4.0)
            # ds = f.create_dataset('volumes/affs', affs.shape)
            # ds[:] = affs
            # ds.attrs['offset'] = common_roi.get_offset()
            # ds.attrs['resolution'] = (40.0,4.0,4.0)
            # ds = f.create_dataset('volumes/labels/gt', gt_with_borders.shape, compression="gzip", dtype=np.uint64)
            # ds[:] = gt_with_borders
            # ds.attrs['offset'] = common_roi.get_offset()
            # ds.attrs['resolution'] = (40.0,4.0,4.0)
            # if fragments_mask is not None:
                # ds = f.create_dataset('volumes/labels/fragments_mask', fragments_mask.shape)
                # ds[:] = fragments_mask
                # ds.attrs['offset'] = common_roi.get_offset()
                # ds.attrs['resolution'] = (40.0,4.0,4.0)
            f.close()

        print "Storing record..."

        metrics = seg_metric[1]
        threshold = thresholds[i]
        i += 1

        print seg_metric
        print metrics

        record = {
            'setup': setup,
            'iteration': iteration,
            'sample': sample,
            'threshold': threshold,
            'merge_function': merge_function,
            'dilate_mask': dilate_mask,
            'mask_fragments': mask_fragments,
            'custom_fragments': custom_fragments,
            'histogram_quantiles': histogram_quantiles,
            'discrete_queue': discrete_queue,
            'raw': { 'filename': gt_filename, 'dataset': 'volumes/raw' },
            'gt': { 'filename': gt_filename, 'dataset': 'volumes/labels/gt' },
            'affinities': { 'filename': affs_filename, 'dataset': 'main' },
            'voi_split': metrics['V_Info_split'],
            'voi_merge': metrics['V_Info_merge'],
            'rand_split': metrics['V_Rand_split'],
            'rand_merge': metrics['V_Rand_merge'],
            'gt_border_threshold': neuron_ids_border_threshold,
            'waterz_version': waterz.__version__,
        }
        with open(output_basename + '.json', 'w') as f:
            json.dump(record, f)


    print "Finished waterz in " + str(time.time() - start) + "s"
