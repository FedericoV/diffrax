from typing import Tuple

import jax

from ..custom_types import Bool, DenseInfo, PyTree, Scalar
from ..local_interpolation import LocalLinearInterpolation
from ..misc import ω
from ..solution import RESULTS
from ..term import AbstractTerm
from .base import AbstractSolver


_ErrorEstimate = None
_SolverState = None


class Euler(AbstractSolver):
    """Euler's method.

    Explicit 1st order RK method. Does not support adaptive timestepping.
    """

    term_structure = jax.tree_structure(0)
    interpolation_cls = LocalLinearInterpolation
    order = 1

    def step(
        self,
        terms: AbstractTerm,
        t0: Scalar,
        t1: Scalar,
        y0: PyTree,
        args: PyTree,
        solver_state: _SolverState,
        made_jump: Bool,
    ) -> Tuple[PyTree, _ErrorEstimate, DenseInfo, _SolverState, RESULTS]:
        del solver_state, made_jump
        control = terms.contr(t0, t1)
        y1 = (y0 ** ω + terms.vf_prod(t0, y0, args, control) ** ω).ω
        dense_info = dict(y0=y0, y1=y1)
        return y1, None, dense_info, None, RESULTS.successful

    def func_for_init(
        self,
        terms: AbstractTerm,
        t0: Scalar,
        y0: PyTree,
        args: PyTree,
    ) -> PyTree:
        return terms.func_for_init(t0, y0, args)
