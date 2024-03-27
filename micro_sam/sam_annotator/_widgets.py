"""Implements the widgets used in the annotation plugins.
"""

import json
import multiprocessing as mp
import os
import pickle
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Literal

import elf.parallel
import h5py
import numpy as np
import zarr
import z5py

from magicgui import magic_factory, widgets
from magicgui.widgets import ComboBox, Container
from napari.qt.threading import thread_worker
from napari.utils import progress
from zarr.errors import PathNotFoundError

from ._state import AnnotatorState
from . import util as vutil
from .. import instance_segmentation, util
from ..multi_dimensional_segmentation import segment_mask_in_volume, merge_instance_segmentation_3d, PROJECTION_MODES

if TYPE_CHECKING:
    import napari


def _select_layer(viewer, layer_name):
    viewer.layers.selection.select_only(viewer.layers[layer_name])


def _reset_tracking_state(viewer):
    """Reset the tracking state.

    This helper function is needed by the widgets clear_track and by commit_track.
    """
    state = AnnotatorState()

    # Reset the lineage and track id.
    state.current_track_id = 1
    state.lineage = {1: []}

    # Reset the layer properties.
    viewer.layers["point_prompts"].property_choices["track_id"] = ["1"]
    viewer.layers["prompts"].property_choices["track_id"] = ["1"]

    # Reset the choices in the track_id menu.
    state.tracking_widget[1].value = "1"
    state.tracking_widget[1].choices = ["1"]


@magic_factory(call_button="Clear Annotations [Shift + C]")
def clear(viewer: "napari.viewer.Viewer") -> None:
    """Widget for clearing the current annotations."""
    vutil.clear_annotations(viewer)


@magic_factory(call_button="Clear Annotations [Shift + C]")
def clear_volume(viewer: "napari.viewer.Viewer", all_slices: bool = True) -> None:
    """Widget for clearing the current annotations in 3D."""
    if all_slices:
        vutil.clear_annotations(viewer)
    else:
        i = int(viewer.cursor.position[0])
        vutil.clear_annotations_slice(viewer, i=i)


@magic_factory(call_button="Clear Annotations [Shift + C]")
def clear_track(viewer: "napari.viewer.Viewer", all_frames: bool = True) -> None:
    """Widget for clearing all tracking annotations and state."""
    if all_frames:
        _reset_tracking_state(viewer)
        vutil.clear_annotations(viewer)
    else:
        i = int(viewer.cursor.position[0])
        vutil.clear_annotations_slice(viewer, i=i)


def _commit_impl(viewer, layer):
    # Check if we have a z_range. If yes, use it to set a bounding box.
    state = AnnotatorState()
    if state.z_range is None:
        bb = np.s_[:]
    else:
        z_min, z_max = state.z_range
        bb = np.s_[z_min:(z_max+1)]

    seg = viewer.layers[layer].data[bb]
    shape = seg.shape

    # We parallelize these operatios because they take quite long for large volumes.

    # Compute the max id in the commited objects.
    # id_offset = int(viewer.layers["committed_objects"].data.max())
    full_shape = viewer.layers["committed_objects"].data.shape
    id_offset = int(
        elf.parallel.max(viewer.layers["committed_objects"].data, block_shape=util.get_block_shape(full_shape))
    )

    # Compute the mask for the current object.
    # mask = seg != 0
    mask = np.zeros(seg.shape, dtype="bool")
    mask = elf.parallel.apply_operation(
        seg, 0, np.not_equal, out=mask, block_shape=util.get_block_shape(shape)
    )

    # Write the current object to committed objects.
    seg[mask] += id_offset
    viewer.layers["committed_objects"].data[bb][mask] = seg[mask]
    viewer.layers["committed_objects"].refresh()

    return id_offset, seg, mask, bb


# TODO also keep track of the model being used and the micro-sam version.
def _commit_to_file(path, viewer, layer, seg, mask, bb, extra_attrs=None):

    # NOTE: Zarr is incredibly inefficient and writes empty blocks.
    # So we have to use z5py here.

    # Deal with issues z5py has with empty folders and require the json.
    if os.path.exists(path):
        required_json = os.path.join(path, ".zgroup")
        if not os.path.exists(required_json):
            with open(required_json, "w") as f:
                json.dump({"zarr_format": 2}, f)

    f = z5py.ZarrFile(path, "a")

    # Write the segmentation.
    full_shape = viewer.layers["committed_objects"].data.shape
    block_shape = util.get_block_shape(full_shape)
    ds = f.require_dataset(
        "committed_objects", shape=full_shape, chunks=block_shape, compression="gzip", dtype=seg.dtype
    )
    ds.n_threads = mp.cpu_count()
    data = ds[bb]
    data[mask] = seg[mask]
    ds[bb] = data

    # Write additional information to attrs.
    if extra_attrs is not None:
        f.attrs.update(extra_attrs)

    # If we run commit from the automatic segmentation we don't have
    # any prompts and so don't need to commit anything else.
    if layer == "auto_segmentation":
        return

    def write_prompts(object_id, prompts, point_prompts):
        g = f.create_group(f"prompts/{object_id}")
        if prompts is not None and len(prompts) > 0:
            data = np.array(prompts)
            g.create_dataset("prompts", data=data, chunks=data.shape)
        if point_prompts is not None and len(point_prompts) > 0:
            g.create_dataset("point_prompts", data=point_prompts, chunks=point_prompts.shape)

    # Commit the prompts for all the objects in the commit.
    object_ids = np.unique(seg[mask])
    if len(object_ids) == 1:  # We only have a single object.
        write_prompts(object_ids[0], viewer.layers["prompts"].data, viewer.layers["point_prompts"].data)
    else:
        have_prompts = len(viewer.layers["prompts"].data) > 0
        have_point_prompts = len(viewer.layers["point_prompts"].data) > 0
        if have_prompts and not have_point_prompts:
            prompts = viewer.layers["prompts"].data
            point_prompts = None
        elif not have_prompts and have_point_prompts:
            prompts = None
            point_prompts = viewer.layers["point_prompts"].data
        else:
            msg = "Got multiple objects from interactive segmentation with box and point prompts." if (
                have_prompts and have_point_prompts
            ) else "Got multiple objects from interactive segmentation with neither box or point prompts."
            raise RuntimeError(msg)

        for i, object_id in enumerate(object_ids):
            write_prompts(
                object_id,
                None if prompts is None else prompts[i:i+1],
                None if point_prompts is None else point_prompts[i:i+1]
            )


@magic_factory(
    call_button="Commit [C]",
    layer={"choices": ["current_object", "auto_segmentation"]},
    commit_path={"mode": "d"},  # choose a directory
)
def commit(
    viewer: "napari.viewer.Viewer",
    layer: str = "current_object",
    commit_path: Optional[Path] = None,
) -> None:
    """Widget for committing the segmented objects from automatic or interactive segmentation."""
    _, seg, mask, bb = _commit_impl(viewer, layer)

    if commit_path is not None:
        _commit_to_file(commit_path, viewer, layer, seg, mask, bb)

    if layer == "current_object":
        vutil.clear_annotations(viewer)
    else:
        viewer.layers["auto_segmentation"].data = np.zeros(
            viewer.layers["auto_segmentation"].data.shape, dtype="uint32"
        )
        viewer.layers["auto_segmentation"].refresh()
        _select_layer(viewer, "committed_objects")


@magic_factory(
    call_button="Commit [C]",
    layer={"choices": ["current_object"]},
    commit_path={"mode": "d"},  # choose a directory
)
def commit_track(
    viewer: "napari.viewer.Viewer",
    layer: str = "current_object",
    commit_path: Optional[Path] = None,
) -> None:
    """Widget for committing the segmented objects from interactive tracking."""
    # Commit the segmentation layer.
    id_offset, seg, mask, bb = _commit_impl(viewer, layer)

    # Update the lineages.
    state = AnnotatorState()
    updated_lineage = {
        parent + id_offset: [child + id_offset for child in children] for parent, children in state.lineage.items()
    }
    state.committed_lineages.append(updated_lineage)

    if commit_path is not None:
        _commit_to_file(
            commit_path, viewer, layer, seg, mask, bb,
            extra_attrs={"committed_lineages": state.committed_lineages}
        )

    if layer == "current_object":
        vutil.clear_annotations(viewer)

    # Reset the tracking state.
    _reset_tracking_state(viewer)


def create_prompt_menu(points_layer, labels, menu_name="prompt", label_name="label"):
    """Create the menu for toggling point prompt labels."""
    label_menu = ComboBox(label=menu_name, choices=labels)
    label_widget = Container(widgets=[label_menu])

    def update_label_menu(event):
        new_label = str(points_layer.current_properties[label_name][0])
        if new_label != label_menu.value:
            label_menu.value = new_label

    points_layer.events.current_properties.connect(update_label_menu)

    def label_changed(new_label):
        current_properties = points_layer.current_properties
        current_properties[label_name] = np.array([new_label])
        points_layer.current_properties = current_properties
        points_layer.refresh_colors()

    label_menu.changed.connect(label_changed)

    return label_widget


def _process_tiling_inputs(tile_shape_x, tile_shape_y, halo_x, halo_y):
    tile_shape = (tile_shape_x, tile_shape_y)
    halo = (halo_x, halo_y)
    # check if tile_shape/halo are not set: (0, 0)
    if all(item == 0 for item in tile_shape):
        tile_shape = None
    # check if at least 1 param is given
    elif tile_shape[0] == 0 or tile_shape[1] == 0:
        max_val = max(tile_shape[0], tile_shape[1])
        if max_val < 256:  # at least tile shape >256
            max_val = 256
        tile_shape = (max_val, max_val)
    # if both inputs given, check if smaller than 256
    elif tile_shape[0] != 0 and tile_shape[1] != 0:
        if tile_shape[0] < 256:
            tile_shape = (256, tile_shape[1])  # Create a new tuple
        if tile_shape[1] < 256:
            tile_shape = (tile_shape[0], 256)  # Create a new tuple with modified value
    if all(item == 0 for item in halo):
        if tile_shape is not None:
            halo = (0, 0)
        else:
            halo = None
    # check if at least 1 param is given
    elif halo[0] != 0 or halo[1] != 0:
        max_val = max(halo[0], halo[1])
        # don't apply halo if there is no tiling
        if tile_shape is None:
            halo = None
        else:
            halo = (max_val, max_val)
    return tile_shape, halo


# TODO add options for tiling, see https://github.com/computational-cell-analytics/micro-sam/issues/331
@magic_factory(
    pbar={"visible": False, "max": 0, "value": 0, "label": "working..."},
    call_button="Compute image embeddings",
    save_path={"mode": "d"},  # choose a directory
    tile_shape_x={"min": 0, "max": 2048},
    tile_shape_y={"min": 0, "max": 2048},
    halo_x={"min": 0, "max": 2048},
    halo_y={"min": 0, "max": 2048},

)
def embedding(
    pbar: widgets.ProgressBar,
    image: "napari.layers.Image",
    model: Literal[tuple(util.models().urls.keys())] = util._DEFAULT_MODEL,
    device: Literal[tuple(["auto"] + util._available_devices())] = "auto",
    save_path: Optional[Path] = None,  # where embeddings for this image are cached (optional)
    custom_weights: Optional[Path] = None,  # A filepath or URL to custom model weights.
    tile_shape_x: int = None,
    tile_shape_y: int = None,
    halo_x: int = None,
    halo_y: int = None,
) -> util.ImageEmbeddings:
    """Widget to compute the embeddings for a napari image layer."""
    state = AnnotatorState()
    state.reset_state()

    # Get image dimensions.
    if image.rgb:
        ndim = image.data.ndim - 1
        state.image_shape = image.data.shape[:-1]
    else:
        ndim = image.data.ndim
        state.image_shape = image.data.shape

    # process tile_shape and halo to tuples or None
    tile_shape, halo = _process_tiling_inputs(tile_shape_x, tile_shape_y, halo_x, halo_y)

    @thread_worker(connect={"started": pbar.show, "finished": pbar.hide})
    def _compute_image_embedding(
        state, image_data, save_path, ndim=None,
        device="auto", model=util._DEFAULT_MODEL,
        custom_weights=None, tile_shape=None, halo=None,
    ):
        # Make sure save directory exists and is an empty directory
        if save_path is not None:
            os.makedirs(save_path, exist_ok=True)
            if not save_path.is_dir():
                raise NotADirectoryError(
                    f"The user selected 'save_path' is not a direcotry: {save_path}"
                )
            if len(os.listdir(save_path)) > 0:
                try:
                    zarr.open(save_path, "r")
                except PathNotFoundError:
                    raise RuntimeError(
                        "The user selected 'save_path' is not a zarr array "
                        f"or empty directory: {save_path}"
                    )

        state.initialize_predictor(
            image_data, model_type=model, save_path=save_path, ndim=ndim, device=device,
            checkpoint_path=custom_weights, tile_shape=tile_shape, halo=halo,
        )
        return state  # returns napari._qt.qthreading.FunctionWorker

    return _compute_image_embedding(
        state, image.data, save_path, ndim=ndim, device=device, model=model,
        custom_weights=custom_weights, tile_shape=tile_shape, halo=halo
    )


@magic_factory(
    call_button="Update settings",
    cache_directory={"mode": "d"},  # choose a directory
)
def settings_widget(
    cache_directory: Optional[Path] = util.get_cache_directory(),
) -> None:
    """Widget to update global micro_sam settings."""
    os.environ["MICROSAM_CACHEDIR"] = str(cache_directory)
    print(f"micro-sam cache directory set to: {cache_directory}")


# TODO fail more gracefully in all widgets if image embeddings have not been initialized
# See https://github.com/computational-cell-analytics/micro-sam/issues/332
#
# Widgets for interactive segmentation:
# - segment: for the 2d annotation tool
# - segment_slice: segment object a single slice for the 3d annotation tool
# - segment_volume: segment object in 3d for the 3d annotation tool
# - segment_frame: segment object in frame for the tracking annotation tool
# - track_object: track object over time for the tracking annotation tool
#


@magic_factory(call_button="Segment Object [S]")
def segment(viewer: "napari.viewer.Viewer", box_extension: float = 0.05, batched: bool = False) -> None:
    shape = viewer.layers["current_object"].data.shape

    # get the current box and point prompts
    boxes, masks = vutil.shape_layer_to_prompts(viewer.layers["prompts"], shape)
    points, labels = vutil.point_layer_to_prompts(viewer.layers["point_prompts"], with_stop_annotation=False)

    predictor = AnnotatorState().predictor
    image_embeddings = AnnotatorState().image_embeddings
    if image_embeddings["original_size"] is None:  # tiled prediction
        seg = vutil.prompt_segmentation(
            predictor, points, labels, boxes, masks, shape, image_embeddings=image_embeddings,
            multiple_box_prompts=True, box_extension=box_extension, multiple_point_prompts=batched,
        )
    else:  # normal prediction
        seg = vutil.prompt_segmentation(
            predictor, points, labels, boxes, masks, shape, multiple_box_prompts=True, box_extension=box_extension,
            multiple_point_prompts=batched,
        )

    # no prompts were given or prompts were invalid, skip segmentation
    if seg is None:
        print("You either haven't provided any prompts or invalid prompts. The segmentation will be skipped.")
        return

    viewer.layers["current_object"].data = seg
    viewer.layers["current_object"].refresh()


@magic_factory(call_button="Segment Slice [S]")
def segment_slice(viewer: "napari.viewer.Viewer", box_extension: float = 0.1) -> None:
    shape = viewer.layers["current_object"].data.shape[1:]
    position = viewer.cursor.position
    z = int(position[0])

    point_prompts = vutil.point_layer_to_prompts(viewer.layers["point_prompts"], z)
    # this is a stop prompt, we do nothing
    if not point_prompts:
        return

    boxes, masks = vutil.shape_layer_to_prompts(viewer.layers["prompts"], shape, i=z)
    points, labels = point_prompts

    state = AnnotatorState()
    seg = vutil.prompt_segmentation(
        state.predictor, points, labels, boxes, masks, shape, multiple_box_prompts=False,
        image_embeddings=state.image_embeddings, i=z, box_extension=box_extension,
    )

    # no prompts were given or prompts were invalid, skip segmentation
    if seg is None:
        print("You either haven't provided any prompts or invalid prompts. The segmentation will be skipped.")
        return

    viewer.layers["current_object"].data[z] = seg
    viewer.layers["current_object"].refresh()


# TODO should probably be wrappred in a thread worker
# See https://github.com/computational-cell-analytics/micro-sam/issues/334
@magic_factory(
    call_button="Segment All Slices [Shift-S]",
    projection={"choices": PROJECTION_MODES},
)
def segment_object(
    viewer: "napari.viewer.Viewer",
    iou_threshold: float = 0.5,
    projection: str = "points",
    box_extension: float = 0.05,
) -> None:
    state = AnnotatorState()
    shape = state.image_shape

    with progress(total=shape[0]) as progress_bar:

        # Step 1: Segment all slices with prompts.
        seg, slices, stop_lower, stop_upper = vutil.segment_slices_with_prompts(
            state.predictor, viewer.layers["point_prompts"], viewer.layers["prompts"],
            state.image_embeddings, shape,
            progress_bar=progress_bar,
        )

        # Step 2: Segment the rest of the volume based on projecting prompts.
        seg, (z_min, z_max) = segment_mask_in_volume(
            seg, state.predictor, state.image_embeddings, slices,
            stop_lower, stop_upper,
            iou_threshold=iou_threshold, projection=projection,
            progress_bar=progress_bar, box_extension=box_extension,
        )

    state.z_range = (z_min, z_max)

    viewer.layers["current_object"].data = seg
    viewer.layers["current_object"].refresh()


def _update_lineage(viewer):
    """Updated the lineage after recording a division event.
    This helper function is needed by 'track_object'.
    """
    state = AnnotatorState()
    tracking_widget = state.tracking_widget

    mother = state.current_track_id
    assert mother in state.lineage
    assert len(state.lineage[mother]) == 0

    daughter1, daughter2 = state.current_track_id + 1, state.current_track_id + 2
    state.lineage[mother] = [daughter1, daughter2]
    state.lineage[daughter1] = []
    state.lineage[daughter2] = []

    # Update the choices in the track_id menu so that it contains the new track ids.
    track_ids = list(map(str, state.lineage.keys()))
    tracking_widget[1].choices = track_ids

    viewer.layers["point_prompts"].property_choices["track_id"] = [str(track_id) for track_id in track_ids]
    viewer.layers["prompts"].property_choices["track_id"] = [str(track_id) for track_id in track_ids]


@magic_factory(call_button="Segment Frame [S]")
def segment_frame(viewer: "napari.viewer.Viewer") -> None:
    state = AnnotatorState()
    shape = state.image_shape[1:]
    position = viewer.cursor.position
    t = int(position[0])

    point_prompts = vutil.point_layer_to_prompts(viewer.layers["point_prompts"], i=t, track_id=state.current_track_id)
    # this is a stop prompt, we do nothing
    if not point_prompts:
        return

    boxes, masks = vutil.shape_layer_to_prompts(viewer.layers["prompts"], shape, i=t, track_id=state.current_track_id)
    points, labels = point_prompts

    seg = vutil.prompt_segmentation(
        state.predictor, points, labels, boxes, masks, shape, multiple_box_prompts=False,
        image_embeddings=state.image_embeddings, i=t
    )

    # no prompts were given or prompts were invalid, skip segmentation
    if seg is None:
        print("You either haven't provided any prompts or invalid prompts. The segmentation will be skipped.")
        return

    # clear the old segmentation for this track_id
    old_mask = viewer.layers["current_object"].data[t] == state.current_track_id
    viewer.layers["current_object"].data[t][old_mask] = 0
    # set the new segmentation
    new_mask = seg.squeeze() == 1
    viewer.layers["current_object"].data[t][new_mask] = state.current_track_id
    viewer.layers["current_object"].refresh()


# TODO should probably be wrappred in a thread worker
@magic_factory(call_button="Track Object [Shift-S]", projection={"choices": PROJECTION_MODES})
def track_object(
    viewer: "napari.viewer.Viewer",
    iou_threshold: float = 0.5,
    projection: str = "points",
    motion_smoothing: float = 0.5,
    box_extension: float = 0.1,
) -> None:
    state = AnnotatorState()
    shape = state.image_shape

    with progress(total=shape[0]) as progress_bar:
        # Step 1: Segment all slices with prompts.
        seg, slices, _, stop_upper = vutil.segment_slices_with_prompts(
            state.predictor, viewer.layers["point_prompts"], viewer.layers["prompts"],
            state.image_embeddings, shape,
            progress_bar=progress_bar, track_id=state.current_track_id
        )

        # Step 2: Track the object starting from the lowest annotated slice.
        seg, has_division = vutil.track_from_prompts(
            viewer.layers["point_prompts"], viewer.layers["prompts"], seg,
            state.predictor, slices, state.image_embeddings, stop_upper,
            threshold=iou_threshold, projection=projection,
            progress_bar=progress_bar, motion_smoothing=motion_smoothing,
            box_extension=box_extension,
        )

    # If a division has occurred and it's the first time it occurred for this track
    # then we need to create the two daughter tracks and update the lineage.
    if has_division and (len(state.lineage[state.current_track_id]) == 0):
        _update_lineage(viewer)

    # clear the old track mask
    viewer.layers["current_object"].data[viewer.layers["current_object"].data == state.current_track_id] = 0
    # set the new object mask
    viewer.layers["current_object"].data[seg == 1] = state.current_track_id
    viewer.layers["current_object"].refresh()


#
# Widgets for automatic segmentation:
# - amg_2d: AMG widget for the 2d annotation tool
# - instace_seg_2d: Widget for instance segmentation with decoder (2d)
# - amg_3d: AMG widget for the 3d annotation tool
# - instace_seg_3d: Widget for instance segmentation with decoder (3d)
#


def _instance_segmentation_impl(viewer, with_background, min_object_size, i=None, skip_update=False, **kwargs):
    state = AnnotatorState()

    if state.amg is None:
        is_tiled = state.image_embeddings["input_size"] is None
        state.amg = instance_segmentation.get_amg(state.predictor, is_tiled, decoder=state.decoder)

    shape = state.image_shape

    # Further optimization: refactor parts of this so that we can also use it in the automatic 3d segmentation fucnction
    # For 3D we store the amg state in a dict and check if it is computed already.
    if state.amg_state is not None:
        assert i is not None
        if i in state.amg_state:
            amg_state_i = state.amg_state[i]
            state.amg.set_state(amg_state_i)

        else:
            dummy_image = np.zeros(shape[-2:], dtype="uint8")
            state.amg.initialize(dummy_image, image_embeddings=state.image_embeddings, verbose=True, i=i)
            amg_state_i = state.amg.get_state()
            state.amg_state[i] = amg_state_i

            cache_folder = state.amg_state.get("cache_folder", None)
            if cache_folder is not None:
                cache_path = os.path.join(cache_folder, f"state-{i}.pkl")
                with open(cache_path, "wb") as f:
                    pickle.dump(amg_state_i, f)

            cache_path = state.amge_state.get("cache_path", None)
            if cache_path is not None:
                save_key = f"state-{i}"
                with h5py.File(cache_path, "a") as f:
                    g = f.create_group(save_key)
                    g.create_dataset("foreground", data=state["foreground"], compression="gzip")
                    g.create_dataset("boundary_distances", data=state["boundary_distances"], compression="gzip")
                    g.create_dataset("center_distances", data=state["center_distances"], compression="gzip")

    # Otherwise (2d segmentation) we just check if the amg is initialized or not.
    elif not state.amg.is_initialized:
        assert i is None
        # We don't need to pass the actual image data here, since the embeddings are passed.
        # (The image data is only used by the amg to compute image embeddings, so not needed here.)
        dummy_image = np.zeros(shape, dtype="uint8")
        state.amg.initialize(dummy_image, image_embeddings=state.image_embeddings, verbose=True)

    seg = state.amg.generate(**kwargs)
    if len(seg) == 0:
        seg = np.zeros(shape[-2:], dtype=viewer.layers["auto_segmentation"].data.dtype)
    else:
        seg = instance_segmentation.mask_data_to_segmentation(
            seg, with_background=with_background, min_object_size=min_object_size
        )
    assert isinstance(seg, np.ndarray)

    if skip_update:
        return seg

    if i is None:
        viewer.layers["auto_segmentation"].data = seg
    else:
        viewer.layers["auto_segmentation"].data[i] = seg
    viewer.layers["auto_segmentation"].refresh()

    return seg


def _segment_volume(viewer, with_background, min_object_size, gap_closing, min_extent, **kwargs):
    segmentation = np.zeros_like(viewer.layers["auto_segmentation"].data)

    offset = 0
    # Further optimization: parallelize if state is precomputed for all slices
    for i in progress(range(segmentation.shape[0]), desc="Segment slices"):
        seg = _instance_segmentation_impl(
            viewer, with_background, min_object_size, i=i, skip_update=True, **kwargs
        )
        seg_max = seg.max()
        if seg_max == 0:
            continue
        seg[seg != 0] += offset
        offset = seg_max + offset
        segmentation[i] = seg

    segmentation = merge_instance_segmentation_3d(
        segmentation, beta=0.5, with_background=with_background, gap_closing=gap_closing,
        min_z_extent=min_extent,
    )

    viewer.layers["auto_segmentation"].data = segmentation
    viewer.layers["auto_segmentation"].refresh()


# TODO should be wrapped in a threadworker
@magic_factory(
    call_button="Automatic Segmentation",
    min_object_size={"min": 0, "max": 10000},
)
def amg_2d(
    viewer: "napari.viewer.Viewer",
    pred_iou_thresh: float = 0.88,
    stability_score_thresh: float = 0.95,
    min_object_size: int = 100,
    box_nms_thresh: float = 0.7,
    with_background: bool = True,
) -> None:
    _instance_segmentation_impl(
        viewer, with_background, min_object_size,
        pred_iou_thresh=pred_iou_thresh, stability_score_thresh=stability_score_thresh,
        box_nms_thresh=box_nms_thresh,
    )
    _select_layer(viewer, "auto_segmentation")


# TODO do we expose additional params?
# TODO should be wrapped in a threadworker
@magic_factory(
    call_button="Automatic Segmentation",
    min_object_size={"min": 0, "max": 10000},
)
def instance_seg_2d(
    viewer: "napari.viewer.Viewer",
    center_distance_threshold: float = 0.5,
    boundary_distance_threshold: float = 0.5,
    min_object_size: int = 100,
    with_background: bool = True,
) -> None:
    _instance_segmentation_impl(
        viewer, with_background, min_object_size, min_size=min_object_size,
        center_distance_threshold=center_distance_threshold,
        boundary_distance_threshold=boundary_distance_threshold,
    )
    _select_layer(viewer, "auto_segmentation")


# TODO should be wrapped in a threadworker
@magic_factory(
    call_button="Automatic Segmentation",
    min_object_size={"min": 0, "max": 10000}
)
def amg_3d(
    viewer: "napari.viewer.Viewer",
    pred_iou_thresh: float = 0.88,
    stability_score_thresh: float = 0.95,
    min_object_size: int = 100,
    box_nms_thresh: float = 0.7,
    with_background: bool = True,
    apply_to_volume: bool = False,
    gap_closing: int = 2,
    min_extent: int = 2,
) -> None:
    if apply_to_volume:
        # We refuse to run 3D segmentation with the AMG unless we have a GPU or all embeddings
        # are precomputed. Otherwise this would take too long.
        state = AnnotatorState()
        predictor = state.predictor
        if str(predictor.device) == "cpu" or str(predictor.device) == "mps":
            n_slices = viewer.layers["auto_segmentation"].data.shape[0]
            embeddings_are_precomputed = len(state.amg_state) > n_slices
            if not embeddings_are_precomputed:
                print("Volumetric segmentation with AMG is only supported if you have a GPU.")
                return
        _segment_volume(
            viewer, with_background, min_object_size, gap_closing,
            pred_iou_thresh=pred_iou_thresh, stability_score_thresh=stability_score_thresh,
            box_nms_thresh=box_nms_thresh, min_extent=min_extent,
        )
    else:
        i = int(viewer.cursor.position[0])
        _instance_segmentation_impl(
            viewer, with_background, min_object_size, i=i,
            pred_iou_thresh=pred_iou_thresh, stability_score_thresh=stability_score_thresh,
            box_nms_thresh=box_nms_thresh,
        )
    _select_layer(viewer, "auto_segmentation")


# TODO do we expose additional params?
# TODO should be wrapped in a threadworker
@magic_factory(
    call_button="Automatic Segmentation",
    min_object_size={"min": 0, "max": 10000},
)
def instance_seg_3d(
    viewer: "napari.viewer.Viewer",
    center_distance_threshold: float = 0.5,
    boundary_distance_threshold: float = 0.5,
    min_object_size: int = 100,
    with_background: bool = True,
    apply_to_volume: bool = False,
    gap_closing: int = 2,
    min_extent: int = 2,
) -> None:
    if apply_to_volume:
        _segment_volume(
            viewer, with_background, min_object_size, gap_closing,
            min_extent=min_extent, min_size=min_object_size,
            center_distance_threshold=center_distance_threshold,
            boundary_distance_threshold=boundary_distance_threshold,
        )
    else:
        i = int(viewer.cursor.position[0])
        _instance_segmentation_impl(
            viewer, with_background, min_object_size, i=i,
            min_size=min_object_size,
            center_distance_threshold=center_distance_threshold,
            boundary_distance_threshold=boundary_distance_threshold,
        )
    _select_layer(viewer, "auto_segmentation")
