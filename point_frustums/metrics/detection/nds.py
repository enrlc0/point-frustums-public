from collections.abc import Sequence
from typing import Any, Optional

import torch
from torchmetrics import Metric
from torchmetrics.utilities import dim_zero_cat

from point_frustums.config_dataclasses.dataset import Annotations
from ..functional.nds import (
    _nds_update_distance_function,
    _nds_update_class_match_function,
    _nds_update_target_count,
    _calc_tp_err_attribute,
    _calc_tp_err_velocity,
    _calc_tp_err_orientation,
    _calc_tp_err_scale,
    _calc_tp_err_translation,
    _nds_update_assign_target,
    _nds_compute_merge_tp_and_fp,
)


class NuScenesDetectionScore(Metric):
    tp_metrics_specification = {
        "translation": {"err_fn": _calc_tp_err_translation, "attribute": "center", "id": "ATE"},
        "scale": {"err_fn": _calc_tp_err_scale, "attribute": "wlh", "id": "ASE"},
        "orientation": {"err_fn": _calc_tp_err_orientation, "attribute": "orientation", "id": "AOE"},
        "velocity": {"err_fn": _calc_tp_err_velocity, "attribute": "velocity", "id": "AVE"},
        "attribute": {"err_fn": _calc_tp_err_attribute, "attribute": "attribute", "id": "AAE"},
    }

    def __init__(
        self,
        annotations: Annotations,
        distance_thresholds: Sequence[float] = (0.5, 1.0, 2.0, 4.0),
        tp_threshold: float = 2.0,
        n_classes: int = 10,
        n_points_interpolation: int = 101,
        min_recall: float = 0.1,
        min_precision: float = 0.1,
        nds_map_weight: float = 5.0,
    ):
        assert tp_threshold in distance_thresholds
        super().__init__()
        # TODO: Steps involved in calculating the NDS
        # TODO: Problem: How to track samples? -> Maybe store dataloader indices
        # TODO: Detection Config
        #   - Filter by {range, bike-rack, n_points(targets) == 0}
        #   - max_boxes per sample
        #   - Compute orientation error for traffic cone only up to 180 deg
        self.annotations = annotations
        self.distance_thresholds = distance_thresholds
        self.tp_threshold = tp_threshold
        self.n_classes = n_classes
        self.n_points_interpolation = n_points_interpolation
        self.min_recall = min_recall
        self.min_precision = min_precision
        self.nds_map_weight = nds_map_weight

        for i_cls in range(self.n_classes):
            # Register the target count state for each class
            self.add_state(f"n_targets_{i_cls}", default=torch.tensor(0), dist_reduce_fx="sum")

        for threshold, _ in enumerate(self.distance_thresholds):
            # Register states applied to all parsed detections
            self.add_state(f"tp_score_t{threshold}", default=[], dist_reduce_fx=None)
            self.add_state(f"tp_class_t{threshold}", default=[], dist_reduce_fx=None)
            self.add_state(f"fp_score_t{threshold}", default=[], dist_reduce_fx=None)
            self.add_state(f"fp_class_t{threshold}", default=[], dist_reduce_fx=None)
            # Register states applied to TP detections (error metrics)
            for metric in self.tp_metrics_specification:
                self.add_state(f"tp_err_{metric}_t{threshold}", default=[], dist_reduce_fx=None)

    def n_targets(self, i: int) -> torch.Tensor:
        return getattr(self, f"n_targets_{i}")

    def _append_list_state(self, reference: str, threshold: int, data: torch.Tensor):
        getattr(self, f"{reference}_t{threshold}").append(data)

    def _increment_target_counts(self, cls_index: list[int], cls_count: list[int]):
        for i, c in zip(cls_index, cls_count):
            n_targets = getattr(self, f"n_targets_{i}")
            n_targets += c

    def update(  # pylint: disable=arguments-differ
        self,
        batch_detections: list[dict[str, torch.Tensor]],
        batch_targets: list[dict[str, torch.Tensor]],
    ):
        """
        Evaluate TP and FP detections for each distance threshold and append to the respective state. Increment the
        target count per class.
        :param batch_detections:
        :param batch_targets:
        :return:
        """
        # Iterate over the samples
        for detections, targets in zip(batch_detections, batch_targets):
            # Store the number of targets per class
            self._increment_target_counts(*_nds_update_target_count(targets["class"]))

            # Evaluate the distance between all detections and all targets and the respective class matches
            distance = _nds_update_distance_function(detections=detections["center"], targets=targets["center"])
            class_match = _nds_update_class_match_function(detections=detections["class"], targets=targets["class"])

            # Iterate over the distance thresholds
            for i, threshold in enumerate(self.distance_thresholds):
                # Assign each detection a target is possible, otherwise the returned distance is inf
                match_distance, match_idx = _nds_update_assign_target(
                    threshold, distance, class_match, score=detections["score"]
                )

                # Append the false positive state
                mask_fp = torch.isinf(match_distance)
                self._append_list_state(reference="fp_class", threshold=i, data=detections["class"][mask_fp])
                self._append_list_state(reference="fp_score", threshold=i, data=detections["score"][mask_fp])

                # Create the TP mask and an index that assigns targets the TP detections
                mask_tp = ~mask_fp
                target_idx = match_idx[mask_tp]
                # Append the true positive state
                self._append_list_state(reference="tp_class", threshold=i, data=detections["class"][mask_tp])
                self._append_list_state(reference="tp_score", threshold=i, data=detections["score"][mask_tp])
                # Append the true positive metrics
                for metric, specification in self.tp_metrics_specification.items():
                    err_fn = specification["err_fn"]
                    attribute_name = specification["attribute"]
                    self._append_list_state(
                        reference=f"tp_err_{metric}",
                        threshold=i,
                        data=err_fn(detections[attribute_name][mask_tp], targets[attribute_name][target_idx]),
                    )

    def _get_cat_state(self, state):
        return dim_zero_cat(getattr(self, state))

    def compute(self, output_file: Optional[str] = None) -> Any:
        interpolated_metrics = {}
        interpolated_metrics_mean = {}

        for i_thresh, d in enumerate(self.distance_thresholds):
            interpolated_metrics[i_thresh] = {}
            interpolated_metrics_mean[i_thresh] = {}

            tp_score = self._get_cat_state(f"tp_score_t{i_thresh}")
            tp_class = self._get_cat_state(f"tp_class_t{i_thresh}")
            fp_score = self._get_cat_state(f"fp_score_t{i_thresh}")
            fp_class = self._get_cat_state(f"fp_class_t{i_thresh}")

            for i_cls in range(self.n_classes):
                interpolated_metrics[i_thresh][i_cls] = {}
                interpolated_metrics_mean[i_thresh][i_cls] = {}
                tp, fp, score, tp_subset_sort_idx = _nds_compute_merge_tp_and_fp(
                    tp_class, tp_score, fp_class, fp_score, i_cls
                )
