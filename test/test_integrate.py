import math
import operator

import diffrax
import equinox as eqx
import jax
import jax.numpy as jnp
import jax.random as jrandom
import pytest
import scipy.stats

from helpers import all_ode_solvers, random_pytree, shaped_allclose, treedefs


@pytest.mark.parametrize(
    "solver_ctr",
    (
        diffrax.Euler,
        diffrax.LeapfrogMidpoint,
        diffrax.ReversibleHeun,
        diffrax.Tsit5,
        diffrax.ImplicitEuler,
        diffrax.Kvaerno3,
    ),
)
@pytest.mark.parametrize("t_dtype", (int, float, jnp.int32, jnp.float32))
@pytest.mark.parametrize("treedef", treedefs)
@pytest.mark.parametrize(
    "stepsize_controller", (diffrax.ConstantStepSize(), diffrax.PIDController(atol=1e2))
)
def test_basic(solver_ctr, t_dtype, treedef, stepsize_controller, getkey):
    if not issubclass(solver_ctr, diffrax.AbstractAdaptiveSolver) and isinstance(
        stepsize_controller, diffrax.PIDController
    ):
        return

    def f(t, y, args):
        return jax.tree_map(operator.neg, y)

    if t_dtype is int:
        t0 = 0
        t1 = 2
        dt0 = 1
    elif t_dtype is float:
        t0 = 0.0
        t1 = 2.0
        dt0 = 1.0
    elif t_dtype is jnp.int32:
        t0 = jnp.array(0)
        t1 = jnp.array(2)
        dt0 = jnp.array(1)
    elif t_dtype is jnp.float32:
        t0 = jnp.array(0.0)
        t1 = jnp.array(2.0)
        dt0 = jnp.array(1.0)
    else:
        raise ValueError
    y0 = random_pytree(getkey(), treedef)
    try:
        diffrax.diffeqsolve(
            diffrax.ODETerm(f),
            solver_ctr(),
            t0,
            t1,
            dt0,
            y0,
            stepsize_controller=stepsize_controller,
        )
    except RuntimeError as e:
        if isinstance(stepsize_controller, diffrax.ConstantStepSize) and str(
            e
        ).startswith("Implicit"):
            # Implicit method failed to converge. A very normal thing to have happen;
            # usually we'd use adaptive timestepping to handle it.
            pass
        else:
            raise


@pytest.mark.parametrize("solver_ctr", all_ode_solvers)
def test_ode_order(solver_ctr):
    key = jrandom.PRNGKey(5678)
    akey, ykey = jrandom.split(key, 2)

    A = jrandom.normal(akey, (10, 10), dtype=jnp.float64) * 0.5

    def f(t, y, args):
        return A @ y

    term = diffrax.ODETerm(f)
    solver = solver_ctr()
    t0 = 0
    t1 = 4
    y0 = jrandom.normal(ykey, (10,), dtype=jnp.float64)

    true_yT = jax.scipy.linalg.expm((t1 - t0) * A) @ y0
    exponents = []
    errors = []
    for exponent in [0, -1, -2, -3, -4, -6, -8, -12]:
        dt0 = 2**exponent
        sol = diffrax.diffeqsolve(term, solver, t0, t1, dt0, y0, max_steps=None)
        yT = sol.ys[-1]
        error = jnp.sum(jnp.abs(yT - true_yT))
        if error < 2**-28:
            break
        exponents.append(exponent)
        errors.append(jnp.log2(error))

    order = scipy.stats.linregress(exponents, errors).slope
    # We accept quite a wide range. Improving this test would be nice.
    assert -0.9 < order - solver.order(term) < 0.9


def _squareplus(x):
    return 0.5 * (x + jnp.sqrt(x**2 + 4))


def _solvers():
    # solver, commutative, order
    yield diffrax.Euler, False, 0.5
    yield diffrax.EulerHeun, False, 0.5
    yield diffrax.Heun, False, 0.5
    yield diffrax.ItoMilstein, False, 0.5
    yield diffrax.Midpoint, False, 0.5
    yield diffrax.ReversibleHeun, False, 0.5
    yield diffrax.StratonovichMilstein, False, 0.5
    yield diffrax.ReversibleHeun, True, 1
    yield diffrax.StratonovichMilstein, True, 1


@pytest.mark.parametrize("solver_ctr,commutative,theoretical_order", _solvers())
def test_sde_strong_order(solver_ctr, commutative, theoretical_order):
    key = jrandom.PRNGKey(5678)
    driftkey, diffusionkey, ykey, bmkey = jrandom.split(key, 4)

    if commutative:
        noise_dim = 1
    else:
        noise_dim = 5

    def drift(t, y, args):
        mlp = eqx.nn.MLP(
            in_size=3,
            out_size=3,
            width_size=8,
            depth=1,
            activation=_squareplus,
            key=driftkey,
        )
        return 0.5 * mlp(y)

    def diffusion(t, y, args):
        mlp = eqx.nn.MLP(
            in_size=3,
            out_size=3 * noise_dim,
            width_size=8,
            depth=1,
            activation=_squareplus,
            final_activation=jnp.tanh,
            key=diffusionkey,
        )
        return 0.25 * mlp(y).reshape(3, noise_dim)

    t0 = 0
    t1 = 2
    y0 = jrandom.normal(ykey, (3,), dtype=jnp.float64)
    bm = diffrax.VirtualBrownianTree(
        t0=t0, t1=t1, shape=(noise_dim,), tol=2**-15, key=bmkey
    )
    if solver_ctr.term_structure == jax.tree_structure(0):
        terms = diffrax.MultiTerm(
            diffrax.ODETerm(drift), diffrax.ControlTerm(diffusion, bm)
        )
    else:
        terms = (diffrax.ODETerm(drift), diffrax.ControlTerm(diffusion, bm))

    # Reference solver is always an ODE-viable solver, so its implementation has been
    # verified by the ODE tests like test_ode_order.
    if issubclass(solver_ctr, diffrax.AbstractItoSolver):
        ref_solver = diffrax.Euler()
    elif issubclass(solver_ctr, diffrax.AbstractStratonovichSolver):
        ref_solver = diffrax.Heun()
    else:
        assert False
    ref_terms = diffrax.MultiTerm(
        diffrax.ODETerm(drift), diffrax.ControlTerm(diffusion, bm)
    )
    true_sol = diffrax.diffeqsolve(
        ref_terms, ref_solver, t0, t1, dt0=2**-14, y0=y0, max_steps=None
    )
    true_yT = true_sol.ys[-1]

    exponents = []
    errors = []
    for exponent in [-3, -4, -5, -6, -7, -8, -9, -10]:
        dt0 = 2**exponent
        sol = diffrax.diffeqsolve(terms, solver_ctr(), t0, t1, dt0, y0, max_steps=None)
        yT = sol.ys[-1]
        error = jnp.sum(jnp.abs(yT - true_yT))
        if error < 2**-28:
            break
        exponents.append(exponent)
        errors.append(jnp.log2(error))

    order = scipy.stats.linregress(exponents, errors).slope
    assert -0.2 < order - theoretical_order < 0.2


# Step size deliberately chosen not to divide the time interval
@pytest.mark.parametrize(
    "solver_ctr,dt0",
    ((diffrax.Euler, -0.3), (diffrax.Tsit5, -0.3), (diffrax.Tsit5, None)),
)
@pytest.mark.parametrize(
    "saveat",
    (
        diffrax.SaveAt(t0=True),
        diffrax.SaveAt(t1=True),
        diffrax.SaveAt(ts=[3.5, 0.7]),
        diffrax.SaveAt(steps=True),
        diffrax.SaveAt(dense=True),
    ),
)
def test_reverse_time(solver_ctr, dt0, saveat, getkey):
    key = getkey()
    y0 = jrandom.normal(key, (2, 2))
    stepsize_controller = (
        diffrax.PIDController() if dt0 is None else diffrax.ConstantStepSize()
    )

    def f(t, y, args):
        return -y

    t0 = 4
    t1 = 0.3
    sol1 = diffrax.diffeqsolve(
        diffrax.ODETerm(f),
        solver_ctr(),
        t0,
        t1,
        dt0,
        y0,
        stepsize_controller=stepsize_controller,
        saveat=saveat,
    )
    assert shaped_allclose(sol1.t0, 4)
    assert shaped_allclose(sol1.t1, 0.3)

    def f(t, y, args):
        return y

    t0 = -4
    t1 = -0.3
    negdt0 = None if dt0 is None else -dt0
    if saveat.ts is not None:
        saveat = diffrax.SaveAt(ts=[-ti for ti in saveat.ts])
    sol2 = diffrax.diffeqsolve(
        diffrax.ODETerm(f),
        solver_ctr(),
        t0,
        t1,
        negdt0,
        y0,
        stepsize_controller=stepsize_controller,
        saveat=saveat,
    )
    assert shaped_allclose(sol2.t0, -4)
    assert shaped_allclose(sol2.t1, -0.3)

    if saveat.t0 or saveat.t1 or saveat.ts is not None or saveat.steps:
        assert shaped_allclose(sol1.ts, -sol2.ts, equal_nan=True)
        assert shaped_allclose(sol1.ys, sol2.ys, equal_nan=True)
    if saveat.dense:
        t = jnp.linspace(0.3, 4, 20)
        for ti in t:
            assert shaped_allclose(sol1.evaluate(ti), sol2.evaluate(-ti))
            assert shaped_allclose(sol1.derivative(ti), -sol2.derivative(-ti))


@pytest.mark.parametrize(
    "solver_ctr,stepsize_controller,dt0",
    (
        (diffrax.Tsit5, diffrax.ConstantStepSize(), 0.3),
        (diffrax.Tsit5, diffrax.PIDController(rtol=1e-8, atol=1e-8), None),
        (diffrax.Kvaerno3, diffrax.PIDController(rtol=1e-8, atol=1e-8), None),
    ),
)
@pytest.mark.parametrize("treedef", treedefs)
def test_pytree_state(solver_ctr, stepsize_controller, dt0, treedef, getkey):
    term = diffrax.ODETerm(lambda t, y, args: jax.tree_map(operator.neg, y))
    y0 = random_pytree(getkey(), treedef)
    sol = diffrax.diffeqsolve(
        term,
        solver=solver_ctr(),
        t0=0,
        t1=1,
        dt0=dt0,
        y0=y0,
        stepsize_controller=stepsize_controller,
    )
    y1 = sol.ys
    true_y1 = jax.tree_map(lambda x: (x * math.exp(-1))[None], y0)
    assert shaped_allclose(y1, true_y1)


def test_semi_implicit_euler():
    term1 = diffrax.ODETerm(lambda t, y, args: -y)
    term2 = diffrax.ODETerm(lambda t, y, args: y)
    y0 = (1.0, -0.5)
    dt0 = 0.00001
    sol1 = diffrax.diffeqsolve(
        (term1, term2),
        diffrax.SemiImplicitEuler(),
        0,
        1,
        dt0,
        y0,
        max_steps=100000,
    )
    term_combined = diffrax.ODETerm(lambda t, y, args: (-y[1], y[0]))
    sol2 = diffrax.diffeqsolve(term_combined, diffrax.Tsit5(), 0, 1, 0.001, y0)
    assert shaped_allclose(sol1.ys, sol2.ys)


def test_compile_time_steps():
    terms = diffrax.ODETerm(lambda t, y, args: -y)
    y0 = jnp.array([1.0])
    solver = diffrax.Tsit5()

    sol = diffrax.diffeqsolve(
        terms, solver, 0, 1, None, y0, stepsize_controller=diffrax.PIDController()
    )
    assert sol.stats["compiled_num_steps"] is None

    sol = diffrax.diffeqsolve(
        terms, solver, 0, 1, 0.1, y0, stepsize_controller=diffrax.PIDController()
    )
    assert sol.stats["compiled_num_steps"] is None

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        0.1,
        y0,
        stepsize_controller=diffrax.ConstantStepSize(compile_steps=True),
    )
    assert shaped_allclose(sol.stats["compiled_num_steps"], 10)

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        0.1,
        y0,
        stepsize_controller=diffrax.ConstantStepSize(compile_steps=None),
    )
    assert shaped_allclose(sol.stats["compiled_num_steps"], 10)

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        0.1,
        y0,
        stepsize_controller=diffrax.ConstantStepSize(compile_steps=False),
    )
    assert sol.stats["compiled_num_steps"] is None

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        None,
        y0,
        stepsize_controller=diffrax.StepTo([0, 0.3, 0.5, 1], compile_steps=True),
    )
    assert shaped_allclose(sol.stats["compiled_num_steps"], 3)

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        None,
        y0,
        stepsize_controller=diffrax.StepTo([0, 0.3, 0.5, 1], compile_steps=None),
    )
    assert shaped_allclose(sol.stats["compiled_num_steps"], 3)

    sol = diffrax.diffeqsolve(
        terms,
        solver,
        0,
        1,
        None,
        y0,
        stepsize_controller=diffrax.StepTo([0, 0.3, 0.5, 1], compile_steps=False),
    )
    assert sol.stats["compiled_num_steps"] is None

    with pytest.raises(ValueError):
        sol = jax.jit(
            lambda t0: diffrax.diffeqsolve(
                terms,
                solver,
                t0,
                1,
                0.1,
                y0,
                stepsize_controller=diffrax.ConstantStepSize(compile_steps=True),
            )
        )(0)

    sol = jax.jit(
        lambda t0: diffrax.diffeqsolve(
            terms,
            solver,
            t0,
            1,
            0.1,
            y0,
            stepsize_controller=diffrax.ConstantStepSize(compile_steps=None),
        )
    )(0)
    assert sol.stats["compiled_num_steps"] is None

    sol = jax.jit(
        lambda t1: diffrax.diffeqsolve(
            terms,
            solver,
            0,
            t1,
            0.1,
            y0,
            stepsize_controller=diffrax.ConstantStepSize(compile_steps=None),
        )
    )(1)
    assert sol.stats["compiled_num_steps"] is None

    sol = jax.jit(
        lambda dt0: diffrax.diffeqsolve(
            terms,
            solver,
            0,
            1,
            dt0,
            y0,
            stepsize_controller=diffrax.ConstantStepSize(compile_steps=None),
        )
    )(0.1)
    assert sol.stats["compiled_num_steps"] is None

    # Work around JAX issue #9298
    diffeqsolve_nojit = diffrax.diffeqsolve.__wrapped__

    _t0 = jnp.array([0, 0])
    sol = jax.jit(
        lambda: jax.vmap(
            lambda t0: diffeqsolve_nojit(
                terms,
                solver,
                t0,
                1,
                0.1,
                y0,
                stepsize_controller=diffrax.ConstantStepSize(compile_steps=True),
            )
        )(_t0)
    )()
    assert shaped_allclose(sol.stats["compiled_num_steps"], jnp.array([10, 10]))

    _t1 = jnp.array([1, 2])
    sol = jax.jit(
        lambda: jax.vmap(
            lambda t1: diffeqsolve_nojit(
                terms,
                solver,
                0,
                t1,
                0.1,
                y0,
                stepsize_controller=diffrax.ConstantStepSize(compile_steps=True),
            )
        )(_t1)
    )()
    assert shaped_allclose(sol.stats["compiled_num_steps"], jnp.array([20, 20]))

    _dt0 = jnp.array([0.1, 0.05])
    sol = jax.jit(
        lambda: jax.vmap(
            lambda dt0: diffeqsolve_nojit(
                terms,
                solver,
                0,
                1,
                dt0,
                y0,
                stepsize_controller=diffrax.ConstantStepSize(compile_steps=True),
            )
        )(_dt0)
    )()
    assert shaped_allclose(sol.stats["compiled_num_steps"], jnp.array([20, 20]))


def test_grad_implicit_solve():
    # Check that we work around JAX issue #9374

    term = diffrax.ODETerm(lambda t, y, args: args * y)
    solve = diffrax.Kvaerno5()

    def f(args):
        return jnp.sum(
            diffrax.diffeqsolve(term, solve, t0=0, t1=1, dt0=0.1, y0=1.0, args=args).ys
        )

    grads = jax.grad(f)(1.0)
    assert jnp.isfinite(grads)
