from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from vsaa import Nnedi3
from vsexprtools import ExprOp, combine, norm_expr
from vskernels import Scaler
from vsrgtools import RepairMode, repair
from vstools import EXPR_VARS, ColorRange, CustomIndexError, CustomOverflowError, P, check_ref_clip, scale_8bit, vs

from .helpers import GenericScaler
from .mask import ringing_mask
from .shaders import FSRCNNXShader, FSRCNNXShaderT

__all__ = [
    'MergeScalers',
    'MergedFSRCNNX'
]


class MergeScalers(GenericScaler):
    def __init__(self, *scalers: tuple[type[Scaler] | Scaler, float]) -> None:
        if (l := len(scalers)) < 2:
            raise CustomIndexError(f'Not enough scalers passed! ({l})', self.__class__)
        elif len(scalers) > len(EXPR_VARS):
            raise CustomIndexError(f'Too many scalers passed! ({l})')

        self.scalers = scalers

    def scale(  # type: ignore
        self, clip: vs.VideoNode, width: int, height: int, shift: tuple[float, float] = (0, 0), **kwargs: Any
    ) -> vs.VideoNode:
        scalers, weights = zip(*self.scalers)

        return combine(
            [scaler.scale(clip, width, height, shift, **kwargs) for scaler in scalers],
            ExprOp.ADD, zip(weights, ExprOp.MUL), expr_suffix=[sum(weights), ExprOp.DIV]
        )


@dataclass
class MergedFSRCNNX(GenericScaler):
    strength: int = 80

    overshoot: float | None = None
    undershoot: float | None = None

    limit: RepairMode | bool = True

    operator: Literal[ExprOp.MAX, ExprOp.MIN] | None = ExprOp.MIN
    masked: bool = True

    reference: type[Scaler] | Scaler | vs.VideoNode = Nnedi3(0, opencl=None)

    range_out: ColorRange | None = None

    fsrcnnx_shader: FSRCNNXShaderT = FSRCNNXShader.x56

    def __post_init__(self) -> None:
        if self.strength >= 100:
            raise CustomOverflowError('strength can\'t be more or equal to 100!', self.__class__)
        elif self.strength <= 0:
            raise CustomOverflowError('strength can\'t be less or equal to 0!', self.__class__)

        if self.overshoot is None:
            self.overshoot = self.strength / 100
        if self.undershoot is None:
            self.undershoot = self.overshoot

    def scale(  # type: ignore
        self, clip: vs.VideoNode, width: int, height: int, shift: tuple[float, float] = (0, 0),
        *, ref: vs.VideoNode | None = None, **kwargs: Any
    ) -> vs.VideoNode:
        assert (self.undershoot or self.undershoot == 0) and (self.overshoot or self.overshoot == 0)

        fsrcnnx = self.fsrcnnx_shader.scale(clip, width, height, shift, **kwargs)

        if not ref and isinstance(self.reference, vs.VideoNode):
            ref = self.reference

        if ref and shift != (0, 0):
            ref = self.kernel.shift(ref, shift)

        smooth = ref or self.scaler.scale(clip, width, height, shift)

        check_ref_clip(clip, ref)

        range_out = ColorRange.from_video(clip, False) if self.range_out is None else self.range_out

        fsr_weight = self.strength / 100

        if self.limit is True:
            expression = [
                'x {fsr_weight} * y {ref_weight} * + up!',
                '{overshoot} O!', '{undershoot} U!',
                'up@ z O@ + > z O@ + up@ ? a U@ - < a U@ - up@ z O@ + > z O@ + up@ ? ?'
            ]

            if range_out is ColorRange.LIMITED:
                expression.append(f'{scale_8bit(clip, 16)} {{clamp_max}} clamp')

            merged = norm_expr(
                [fsrcnnx, smooth, smooth.std.Maximum(), smooth.std.Minimum()],
                expression, fsr_weight=fsr_weight, ref_weight=1.0 - fsr_weight,
                undershoot=self.undershoot * (2 ** 8), overshoot=self.overshoot * (2 ** 8),
                clamp_max=[scale_8bit(clip, 235), scale_8bit(clip, 240)]
            )
        else:
            merged = smooth.std.Merge(fsrcnnx, fsr_weight)

            if isinstance(self.limit, RepairMode):
                merged = repair(merged, smooth, self.limit)

        if self.operator is not None:
            merge2 = combine([smooth, fsrcnnx], ExprOp.MIN)

            if self.masked:
                merged = merged.std.MaskedMerge(merge2, ringing_mask(smooth))
            else:
                merged = merge2
        elif self.masked:
            merged.std.MaskedMerge(smooth, ringing_mask(smooth))

        return merged