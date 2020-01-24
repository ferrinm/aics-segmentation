import numpy as np
from scipy.ndimage import distance_transform_edt
from scipy.ndimage.morphology import binary_opening
from skimage.morphology import erosion, ball, medial_axis
import aicsimageio
from skimage.measure import label, regionprops
from skimage.segmentation import find_boundaries

import SimpleITK as sitk

def hole_filling(bw, hole_min, hole_max, fill_2d=True):

    bw = bw>0
    if len(bw.shape)==2:
        background_lab = label(~bw, connectivity=1)
        fill_out = np.copy(background_lab)
        component_sizes = np.bincount(background_lab.ravel())
        too_big = component_sizes >hole_max
        too_big_mask = too_big[background_lab]
        fill_out[too_big_mask] = 0
        too_small = component_sizes <hole_min
        too_small_mask = too_small[background_lab]
        fill_out[too_small_mask] = 0
    elif len(bw.shape)==3:
        if fill_2d:
            fill_out = np.zeros_like(bw)
            for zz in range(bw.shape[0]):
                background_lab = label(~bw[zz,:,:], connectivity=1)
                out = np.copy(background_lab)
                component_sizes = np.bincount(background_lab.ravel())
                too_big = component_sizes >hole_max
                too_big_mask = too_big[background_lab]
                out[too_big_mask] = 0
                too_small = component_sizes < hole_min
                too_small_mask = too_small[background_lab]
                out[too_small_mask] = 0
                fill_out[zz,:,:] = out
        else:
            background_lab = label(~bw, connectivity=1)
            fill_out = np.copy(background_lab)
            component_sizes = np.bincount(background_lab.ravel())
            too_big = component_sizes >hole_max
            too_big_mask = too_big[background_lab]
            fill_out[too_big_mask] = 0
            too_small = component_sizes < hole_min
            too_small_mask = too_small[background_lab]
            fill_out[too_small_mask] = 0
    else:
        print('error')
        return
        
    return np.logical_or(bw, fill_out)

def topology_preserving_thinning(bw, min_thickness=1, thin=1):
    bw = bw>0
    safe_zone = np.zeros_like(bw)
    for zz in range(bw.shape[0]):
        if np.any(bw[zz, :, :]):
            ctl = medial_axis(bw[zz, :, :] > 0)
            dist = distance_transform_edt(ctl == 0)
            safe_zone[zz, :, :] = dist > min_thickness + 1e-5

    rm_candidate = np.logical_xor(bw > 0, erosion(bw > 0, ball(thin)))

    bw[np.logical_and(safe_zone, rm_candidate)] = 0

    return bw


def divide_nonzero(array1, array2):
    """
    Divides two arrays. Returns zero when dividing by zero.
    """
    denominator = np.copy(array2)
    denominator[denominator == 0] = 1e-10
    return np.divide(array1, denominator)


def create_image_like(data, image):
    return image.__class__(data, affine=image.affine, header=image.header)


def histogram_otsu(hist):

    # modify the elements in hist to avoid completely zero value in cumsum
    hist = hist+1e-5

    bin_size = 1/(len(hist)-1)
    bin_centers = np.arange(0, 1+0.5*bin_size, bin_size)
    hist = hist.astype(float)

    # class probabilities for all possible thresholds
    weight1 = np.cumsum(hist)
    weight2 = np.cumsum(hist[::-1])[::-1]
    # class means for all possible thresholds

    mean1 = np.cumsum(hist * bin_centers) / weight1
    mean2 = (np.cumsum((hist * bin_centers)[::-1]) / weight2[::-1])[::-1]

    # Clip ends to align class 1 and class 2 variables:
    # The last value of `weight1`/`mean1` should pair with zero values in
    # `weight2`/`mean2`, which do not exist.
    variance12 = weight1[:-1] * weight2[1:] * (mean1[:-1] - mean2[1:]) ** 2

    idx = np.argmax(variance12)
    threshold = bin_centers[:-1][idx]
    return threshold


def absolute_eigenvaluesh(nd_array):
    """
    Computes the eigenvalues sorted by absolute value from the symmetrical matrix.
    :param nd_array: array from which the eigenvalues will be calculated.
    :return: A list with the eigenvalues sorted in absolute ascending order (e.g. [eigenvalue1, eigenvalue2, ...])
    """
    # print(nd_array)
    # print('up:array, below:eigen')
    eigenvalues = np.linalg.eigvalsh(nd_array)
    # print(eigenvalues)
    sorted_eigenvalues = sortbyabs(eigenvalues, axis=-1)
    return [np.squeeze(eigenvalue, axis=-1)
            for eigenvalue in np.split(sorted_eigenvalues, sorted_eigenvalues.shape[-1], axis=-1)]


def sortbyabs(a, axis=0):
    """Sort array along a given axis by the absolute value
    modified from: http://stackoverflow.com/a/11253931/4067734
    """
    index = list(np.ix_(*[np.arange(i) for i in a.shape]))
    index[axis] = np.abs(a).argsort(axis)
    return a[index]

def get_middle_frame(struct_img_smooth, method='z'):

    from skimage.filters import threshold_otsu

    if method == 'intensity':
        bw = struct_img_smooth>threshold_otsu(struct_img_smooth)
        z_profile = np.zeros((bw.shape[0],),dtype=int)
        for zz in range(bw.shape[0]):
            z_profile[zz] = np.count_nonzero(bw[zz,:,:])
        mid_frame = round(histogram_otsu(z_profile)*bw.shape[0]).astype(int)
        
    elif method == 'z':
        mid_frame = struct_img_smooth.shape[0] // 2

    else:
        print('unsupported method')
        quit()
    
    return mid_frame

def get_3dseed_from_mid_frame(bw, stack_shape, mid_frame, hole_min, bg_seed = True):
    from skimage.morphology import remove_small_objects
    out = remove_small_objects(bw>0, hole_min)

    out1 = label(out)
    stat = regionprops(out1)
    
    # build the seed for watershed
    seed = np.zeros(stack_shape)
    seed_count=0
    if bg_seed:
        seed[0,:,:] = 1
        seed_count += 1

    for idx in range(len(stat)):
        py, px = np.round(stat[idx].centroid)
        seed_count+=1
        seed[mid_frame,int(py),int(px)]=seed_count

    return seed

def levelset_segmentation(smooth_img, seed, niter, max_error, epsilon, curvature_weight, smoothing_weight):
    # This function performs chan vese segmentation
    #
    # initialize level-set
    level0 = find_boundaries(seed, connectivity=1, mode='outer')
    init_levelset = distance_transform_edt(~seed) - distance_transform_edt(seed)
    init_levelset[level0] = 0 
    itk_img = sitk.GetImageFromArray(smooth_img.astype("float")) # Prepare original image

    lsFilter = sitk.ScalarChanAndVeseDenseLevelSetImageFilter()
    lsFilter.SetMaximumRMSError(max_error)
    lsFilter.SetNumberOfIterations(niter)
    lsFilter.SetLambda1(1)
    lsFilter.SetLambda2(1)
    lsFilter.SetEpsilon(epsilon)
    lsFilter.SetCurvatureWeight(curvature_weight)
    lsFilter.SetAreaWeight(0.0)
    lsFilter.SetReinitializationSmoothingWeight(smoothing_weight)
    lsFilter.SetVolume(0.0)
    lsFilter.SetVolumeMatchingWeight(0.0)
    lsFilter.SetHeavisideStepFunction(lsFilter.AtanRegularizedHeaviside)
    ls = lsFilter.Execute(sitk.GetImageFromArray(init_levelset), itk_img)
    out = sitk.GetArrayFromImage(ls>0).astype("uint8")

    # Post processing
    out = hole_filling(out, 100, 1500, fill_2d=False)
    out = binary_opening(out, structure=ball(4)).astype("uint8")

    return out

def fast_marching_levelset_segmentation(smooth_img, seed):
    # This function performs fast marching segmentation
    #
    # Change image format to sitk image format
    itk_img = sitk.GetImageFromArray(smooth_img.astype("float"))

    # set gradient filter
    gradient_filter = sitk.GradientMagnitudeImageFilter()
    gradient = gradient_filter.Execute(itk_img)

    # Now need to invert gradient to make gradient inside object to negative. Use sigmoid function with negative parameter
    sigmoid = sitk.SigmoidImageFilter()
    sigmoid.SetOutputMaximum(1)
    sigmoid.SetOutputMinimum(0)
    sigmoid.SetAlpha(-0.5)
    sigmoid.SetBeta(3.0)
    invert_gradeint = sigmoid.Execute(gradient)

    # Fast marching levelset
    fast_marching = sitk.FastMarchingImageFilter()
    fast_marching.SetTrialPoints(seed)
    fastMarchingOutput = fast_marching.Execute(invert_gradeint)

    # Using binary threshold
    thresholder = sitk.BinaryThresholdImageFilter()
    thresholder.SetLowerThreshold(0.0)
    # thresholder.SetUpperThreshold(timeThreshold)
    thresholder.SetOutsideValue(0)
    thresholder.SetInsideValue(255)

    seg = thresholder.Execute(fastMarchingOutput)

    return seg