"""Routines for numerical differentiation."""

from __future__ import division

import sys

import numpy as np
from ..sparse import issparse, csc_matrix, csr_matrix, lil_matrix, find
from  ._group_columns import group_dense, group_sparse

EPS = np.finfo(np.float64).eps


def _adjust_scheme_to_bounds(x0, h, num_steps, scheme, lb, ub):
    """Adjust final difference scheme to the presence of bounds.

    Parameters
    ----------
    x0 : ndarray, shape (n,)
        Point at which we wish to estimate derivative.
    h : ndarray, shape (n,)
        Desired finite difference steps.
    num_steps : int
        Number of `h` steps in one direction required to implement finite
        difference scheme. For example, 2 means that we need to evaluate
        f(x0 + 2 * h) or f(x0 - 2 * h)
    scheme : {'1-sided', '2-sided'}
        Whether steps in one or both directions are required. In other
        words '1-sided' applies to forward and backward schemes, '2-sided'
        applies to center schemes.
    lb : ndarray, shape (n,)
        Lower bounds on independent variables.
    ub : ndarray, shape (n,)
        Upper bounds on independent variables.

    Returns
    -------
    h_adjusted : ndarray, shape (n,)
        Adjusted step sizes. Step size decreases only if a sign flip or
        switching to one-sided scheme doesn't allow to take a full step.
    use_one_sided : ndarray of bool, shape (n,)
        Whether to switch to one-sided scheme. Informative only for
        ``scheme='2-sided'``.
    """
    if scheme == '1-sided':
        use_one_sided = np.ones_like(h, dtype=bool)
    elif scheme == '2-sided':
        h = np.abs(h)
        use_one_sided = np.zeros_like(h, dtype=bool)
    else:
        raise ValueError("`scheme` must be '1-sided' or '2-sided'.")

    if np.all((lb == -np.inf) & (ub == np.inf)):
        return h, use_one_sided

    h_total = h * num_steps
    h_adjusted = h.copy()

    lower_dist = x0 - lb
    upper_dist = ub - x0

    if scheme == '1-sided':
        x = x0 + h_total
        violated = (x < lb) | (x > ub)
        fitting = np.abs(h_total) <= np.maximum(lower_dist, upper_dist)
        h_adjusted[violated & fitting] *= -1

        forward = (upper_dist >= lower_dist) & ~fitting
        h_adjusted[forward] = upper_dist[forward] / num_steps
        backward = (upper_dist < lower_dist) & ~fitting
        h_adjusted[backward] = -lower_dist[backward] / num_steps
    elif scheme == '2-sided':
        central = (lower_dist >= h_total) & (upper_dist >= h_total)

        forward = (upper_dist >= lower_dist) & ~central
        h_adjusted[forward] = np.minimum(
            h[forward], 0.5 * upper_dist[forward] / num_steps)
        use_one_sided[forward] = True

        backward = (upper_dist < lower_dist) & ~central
        h_adjusted[backward] = -np.minimum(
            h[backward], 0.5 * lower_dist[backward] / num_steps)
        use_one_sided[backward] = True

        min_dist = np.minimum(upper_dist, lower_dist) / num_steps
        adjusted_central = (~central & (np.abs(h_adjusted) <= min_dist))
        h_adjusted[adjusted_central] = min_dist[adjusted_central]
        use_one_sided[adjusted_central] = False

    return h_adjusted, use_one_sided


def _compute_absolute_step(rel_step, x0, method):
    if rel_step is None:
        if method == '2-point':
            rel_step = EPS**0.5
        elif method == '3-point':
            rel_step = EPS**(1 / 3)
        else:
            raise ValueError("`method` must be '2-point' or '3-point'.")

    sign_x0 = (x0 >= 0).astype(float) * 2 - 1
    return rel_step * sign_x0 * np.maximum(1.0, np.abs(x0))


def _prepare_bounds(bounds, x0):
    lb, ub = [np.asarray(b, dtype=float) for b in bounds]
    if lb.ndim == 0:
        lb = np.resize(lb, x0.shape)

    if ub.ndim == 0:
        ub = np.resize(ub, x0.shape)

    return lb, ub


def group_columns(A, order=None):
    """Group columns of a 2-d matrix for sparse finite differencing [1]_.

    Two columns are in the same group if in each row at least one of them
    has zero. A greedy sequential algorithm is used to construct groups.

    Parameters
    ----------
    A : array_like or sparse matrix, shape (m, n)
        Matrix of which to group columns.
    order : None or iterable of int, shape (n,)
        Permutation array which defines the order of columns enumeration.
        If None (default) the order will be random.

    Returns
    -------
    groups : ndarray of int, shape (n,)
        Contains values from 0 to n_groups-1, where n_groups is the number
        of found groups. Each value ``groups[i]`` is an index of a group to
        which i-th column assigned. The procedure was helpful only if
        n_groups is significantly less than n.

    References
    ----------
    .. [1] A. Curtis, M. J. D. Powell, and J. Reid, "On the estimation of
           sparse Jacobian matrices", Journal of the Institute of Mathematics
           and its Applications, 13 (1974), pp. 117-120.
    """
    if issparse(A):
        A = csc_matrix(A)
    else:
        A = np.atleast_2d(A)
        A = (A != 0).astype(np.int32)

    if A.ndim != 2:
        raise ValueError("`A` must be 2-dimensional.")

    m, n = A.shape

    if order is None:
        order = np.arange(n)
        np.random.shuffle(order)

    A = A[:, order]

    if issparse(A):
        groups = group_sparse(m, n, A.indices, A.indptr)
    else:
        groups = group_dense(m, n, A)

    groups[order] = groups.copy()

    return groups


def approx_derivative(fun, x0, method='3-point', rel_step=None, f0=None,
                      args=(), kwargs={}, bounds=(-np.inf, np.inf),
                      sparsity=None):
    """Compute finite difference approximation of the derivatives of a
    vector-valued function.

    If a function maps from R^n to R^m, its derivatives form m-by-n matrix
    called Jacobian, where an element (i, j) is a partial derivative of f[i]
    with respect to x[j].

    Parameters
    ----------
    fun : callable
        Function of which to estimate the derivatives. The argument x
        passed to this function is ndarray of shape (n,) (never a scalar
        even if n=1). It must return 1-d array_like of shape (m,) or a scalar.
    x0 : array_like of shape (n,) or float
        Point at which to estimate the derivatives. Float will be converted
        to a 1-d array.
    method : {'3-point', '2-point'}, optional
        Finite difference method to use:
            - '2-point' - use the fist order accuracy forward or backward
                          difference.
            - '3-point' - use central difference in interior points and the
                          second order accuracy forward or backward difference
                          near the boundary.
    rel_step : None or array_like, optional
        Relative step size to use. The absolute step size is computed as
        ``h = rel_step * sign(x0) * max(1, abs(x0))``, possibly adjusted to
        fit into the bounds. For ``method='3-point'`` the sign of `h` is
        ignored. If None (default) then step is selected automatically,
        see Notes.
    f0 : None or array_like, optional
        If not None it is assumed to be equal to ``fun(x0)``, in  this case
        the ``fun(x0)`` is not called. Default is None.
    args, kwargs : tuple and dict, optional
        Additional arguments passed to `fun`. Both empty by default.
        The calling signature is ``fun(x, *args, **kwargs)``.
    bounds : tuple of array_like, optional
        Lower and upper bounds on independent variables. Defaults to no bounds.
        Each bound must match the size of `x0` or be a scalar, in the latter
        case the bound will be the same for all variables. Use it to limit the
        range of function evaluation.
    sparsity : None or (structure, groups) tuple, optional
        Defines sparsity structure of Jacobian.

        * structure : array_like or sparse matrix of shape (m, n). A zero
          element means that a corresponding element of Jacobian identically
          equals to zero.
        * groups : array_like of shape (n,). Precomputed columns grouping
          for a given sparsity structure, function `group_columns` should be
          used beforehand to compute it.

        Note, that sparse differencing makes sense only for actually large
        and sparse Jacobians. If None (default) standard dense differencing
        will be used.

    Returns
    -------
    J : ndarray or csr_matrix
        Finite difference approximation of derivatives in the form of Jacobian
        matrix. If `sparse` is None then ndarray with shape (m, n) is
        returned. Although if m=1 it is returned as a gradient with
        shape (n,). If `sparse` is not None, csr_matrix with shape (m, n) is
        returned.

    See Also
    --------
    check_derivative : Check correctness of a function computing derivatives.

    Notes
    -----
    If `rel_step` is not provided, it assigned to ``EPS**(1/s)``, where EPS is
    machine epsilon for float64 numbers, s=2 for '2-point' method and s=3 for
    '3-point' method. Such relative step approximately minimizes a sum of
    truncation and round-off errors, see [1]_.

    A finite difference scheme for '3-point' method is selected automatically.
    The well-known central difference scheme is used for points sufficiently
    far from the boundary, and 3-point forward or backward scheme is used for
    points near the boundary. Both schemes have the second-order accuracy in
    terms of Taylor expansion. Refer to [2]_ for the formulas of 3-point
    forward and backward difference schemes.

    For dense differencing when m=1 Jacobian is returned with a shape (n,),
    on the other hand when n=1 Jacobian is returned with a shape (m, 1).
    Our motivation is the following: a) It handles a case of gradient
    computation (m=1) in a conventional way. b) It clearly separates these two
    different cases. b) In all cases np.atleast_2d can be called to get 2-d
    Jacobian with correct dimensions.

    References
    ----------
    .. [1] W. H. Press et. al. "Numerical Recipes. The Art of Scientific
           Computing. 3rd edition", sec. 5.7.

    .. [2] B. Fornberg, "Generation of Finite Difference Formulas on
           Arbitrarily Spaced Grids", Mathematics of Computation 51, 1988.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.optimize import approx_derivative
    >>>
    >>> def f(x, c1, c2):
    ...     return np.array([x[0] * np.sin(c1 * x[1]),
    ...                      x[0] * np.cos(c2 * x[1])])
    ...
    >>> x0 = np.array([1.0, 0.5 * np.pi])
    >>> approx_derivative(f, x0, args=(1, 2))
    array([[ 1.,  0.],
           [-1.,  0.]])

    Bounds can be used to limit the region of function evaluation.
    In the example below we compute left and right derivative at point 1.0.

    >>> def g(x):
    ...     return x**2 if x >= 1 else x
    ...
    >>> x0 = 1.0
    >>> approx_derivative(g, x0, bounds=(-np.inf, 1.0))
    array([ 1.])
    >>> approx_derivative(g, x0, bounds=(1.0, np.inf))
    array([ 2.])
    """
    if method not in ['2-point', '3-point']:
        raise ValueError("`method` must be '2-point' or '3-point'.")

    x0 = np.atleast_1d(x0)
    if x0.ndim > 1:
        raise ValueError("`x0` must have at most 1 dimension.")

    lb, ub = _prepare_bounds(bounds, x0)

    if lb.shape != x0.shape or ub.shape != x0.shape:
        raise ValueError("Inconsistent shapes between bounds and `x0`.")

    def fun_wrapped(x):
        f = np.atleast_1d(fun(x, *args, **kwargs))
        if f.ndim > 1:
            raise RuntimeError(("`fun` return value has "
                                "more than 1 dimension."))
        return f

    if f0 is None:
        f0 = fun_wrapped(x0)
    else:
        f0 = np.atleast_1d(f0)
        if f0.ndim > 1:
            raise ValueError("`f0` passed has more than 1 dimension.")

    if np.any((x0 < lb) | (x0 > ub)):
        raise ValueError("`x0` violates bound constraints.")

    h = _compute_absolute_step(rel_step, x0, method)

    if method == '2-point':
        h, use_one_sided = _adjust_scheme_to_bounds(
            x0, h, 1, '1-sided', lb, ub)
    elif method == '3-point':
        h, use_one_sided = _adjust_scheme_to_bounds(
            x0, h, 1, '2-sided', lb, ub)

    if sparsity is None:
        return _dense_difference(fun_wrapped, x0, f0, h, use_one_sided, method)
    else:
        structure, groups = sparsity
        if issparse(structure):
            structure = csc_matrix(structure)
        else:
            structure = np.atleast_2d(structure)
        groups = np.atleast_1d(groups)
        return _sparse_difference(fun_wrapped, x0, f0, h, use_one_sided,
                                  structure, groups, method)


def _dense_difference(fun, x0, f0, h, use_one_sided, method):
    m = f0.size
    n = x0.size
    J_transposed = np.empty((n, m))
    h_vecs = np.diag(h)

    for i in range(h.size):
        if method == '2-point':
            x = x0 + h_vecs[i]
            dx = x[i] - x0[i]  # recompute dx as exactly representable number
            df = fun(x) - f0
        elif method == '3-point' and use_one_sided[i]:
            x1 = x0 + h_vecs[i]
            x2 = x0 + 2 * h_vecs[i]
            dx = x2[i] - x0[i]
            f1 = fun(x1)
            f2 = fun(x2)
            df = -3.0 * f0 + 4 * f1 - f2
        elif method == '3-point' and not use_one_sided[i]:
            x1 = x0 - h_vecs[i]
            x2 = x0 + h_vecs[i]
            dx = x2[i] - x1[i]
            f1 = fun(x1)
            f2 = fun(x2)
            df = f2 - f1
        J_transposed[i] = df / dx

    if m == 1:
        J_transposed = np.ravel(J_transposed)

    return J_transposed.T


def _sparse_difference(fun, x0, f0, h, use_one_sided,
                       structure, groups, method):
    m = f0.size
    n = x0.size
    J = lil_matrix((m, n), dtype=float)
    n_groups = np.max(groups) + 1
    for group in range(n_groups):
        e = np.equal(group, groups)
        h_vec = h * e
        if method == '2-point':
            x = x0 + h_vec
            dx = x - x0
            df = fun(x) - f0
            cols, = np.where(e)
            i, j, v = find(structure[:, cols])
            j = cols[j]
        elif method == '3-point':
            x1 = x0.copy()
            x2 = x0.copy()

            mask_1 = use_one_sided & e
            x1[mask_1] += h_vec[mask_1]
            x2[mask_1] += 2 * h_vec[mask_1]

            mask_2 = ~use_one_sided & e
            x1[mask_2] -= h_vec[mask_2]
            x2[mask_2] += h_vec[mask_2]

            dx = np.zeros(n)
            dx[mask_1] = x2[mask_1] - x0[mask_1]
            dx[mask_2] = x2[mask_2] - x1[mask_2]

            f1 = fun(x1)
            f2 = fun(x2)

            cols, = np.where(e)
            i, j, _ = find(structure[:, cols])
            j = cols[j]

            mask = use_one_sided[j]
            df = np.empty(m)

            rows = i[mask]
            df[rows] = -3 * f0[rows] + 4 * f1[rows] - f2[rows]

            rows = i[~mask]
            df[rows] = f2[rows] - f1[rows]

        J[i, j] = df[i] / dx[j]

    return csr_matrix(J)


def check_derivative(fun, jac, x0, args=(), kwargs={},
                     bounds=(-np.inf, np.inf), sparse_diff=False,
                     sparsity=None):
    """Check correctness of a function computing derivatives (Jacobian or
    gradient) by comparison with finite difference approximation.

    Parameters
    ----------
    fun : callable
        Function of which to estimate the derivatives. The argument x
        passed to this function is ndarray of shape (n,) (never a scalar
        even if n=1). It must return 1-d array_like of shape (m,) or a scalar.
    jac : callable
        Function which computes Jacobian matrix of `fun`. It must work with
        argument x the same way as `fun`. The return value must be array_like
        with an appropriate shape.
    x0 : array_like of shape (n,) or float
        Point at which to estimate the derivatives. Float will be converted
        to 1-d array.
    args, kwargs : tuple and dict, optional
        Additional arguments passed to `fun` and `jac`. Both empty by default.
        The calling signature is ``fun(x, *args, **kwargs)`` and the same
        for `jac`.
    bounds : 2-tuple of array_like, optional
        Lower and upper bounds on independent variables. Defaults to no bounds.
        Each bound must match the size of `x0` or be a scalar, in the latter
        case the bound will be the same for all variables. Use it to limit the
        range of function evaluation.
    sparse_diff : boolean, optional
        If `jac` returns a sparse matrix this flag indicates whether to
        use sparse differencing for Jacobian estimation.
    sparsity : None or (structure, groups) tuple, optional
        If `jac` returns a sparse matrix and ``sparse_diff=True``, provide a
        tuple with following elements:

        * structure : array_like or sparse matrix of shape (m, n). A zero
          element means that a corresponding element of Jacobian identically
          equals to zero.
        * groups : array_like of shape (n,). Precomputed columns grouping
          for a given sparsity structure, function `group_columns` should be
          used beforehand to compute it.

        If None `structure` will be taken from evaluated `jac` and `groups`
        will be computed.

    Returns
    -------
    accuracy : float
        The maximum among all relative errors for elements with absolute values
        higher than 1 and absolute errors for elements with absolute values
        less or equal than 1. If `accuracy` is on the order of 1e-6 or lower,
        then it is likely that your `jac` implementation is correct.

    See Also
    --------
    approx_derivative : Compute finite difference approximation of derivative.

    Examples
    --------
    >>> import numpy as np
    >>> from scipy.optimize import check_derivative
    >>>
    >>>
    >>> def f(x, c1, c2):
    ...     return np.array([x[0] * np.sin(c1 * x[1]),
    ...                      x[0] * np.cos(c2 * x[1])])
    ...
    >>> def jac(x, c1, c2):
    ...     return np.array([
    ...         [np.sin(c1 * x[1]),  c1 * x[0] * np.cos(c1 * x[1])],
    ...         [np.cos(c2 * x[1]), -c2 * x[0] * np.sin(c2 * x[1])]
    ...     ])
    ...
    >>>
    >>> x0 = np.array([1.0, 0.5 * np.pi])
    >>> check_derivative(f, jac, x0, args=(1, 2))
    2.4492935982947064e-16
    """
    J_to_test = jac(x0, *args, **kwargs)
    if issparse(J_to_test) and sparse_diff:
        if sparsity is None:
            groups = group_columns(J_to_test)
            sparsity = (J_to_test, groups)
        J_diff = approx_derivative(fun, x0, args=args, kwargs=kwargs,
                                   bounds=bounds, sparsity=sparsity)
        J_to_test = csr_matrix(J_to_test)
        abs_err = J_to_test - J_diff
        i, j, abs_err_data = find(abs_err)
        jac_diff_data = np.asarray(J_diff[i, j]).ravel()
        return np.max(np.abs(abs_err_data) /
                      np.maximum(1, np.abs(jac_diff_data)))
    else:
        J_diff = approx_derivative(
            fun, x0, args=args, kwargs=kwargs, bounds=bounds)
        if issparse(J_to_test):
            J_to_test = J_to_test.toarray()
        abs_err = np.abs(J_to_test - J_diff)
        return np.max(abs_err / np.maximum(1, np.abs(J_diff)))
