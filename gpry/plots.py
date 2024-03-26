import warnings
from typing import Sequence, Mapping
from numbers import Number

import numpy as np
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import cm

from gpry.gpr import GaussianProcessRegressor
from gpry.tools import (
    credibility_of_nstd,
    nstd_of_1d_nstd,
    volume_sphere,
    gaussian_distance,
)

# Use latex labels when available
plt.rcParams["text.usetex"] = True


def param_samples_for_slices(X, i, bounds, n=200):
    """
    From an array of points `X = [X^i] = [[X^1_1, X^1_2,...], [X^2_1,...], ...]`, it
    generates a list of points per sample, where the `i` coordinate is sliced within the
    region defined by `bounds`, and the rest of them are kept fixed.
    """
    # TODO: could take a GPR, reduce the limits to finite region, and resample.
    #       The while loop is a leftover from an attempt.
    X = np.atleast_2d(X)
    bounds_changed = True
    while bounds_changed:
        Xs_i = np.linspace(bounds[0], bounds[1], n)
        X_slices = np.empty(shape=(X.shape[0], n, X.shape[1]), dtype=float)
        for j, X_j in enumerate(X):
            X_slices[j, :, :] = n * [X_j]
        X_slices[:, :, i] = Xs_i
        break
    return X_slices


def plot_slices(model, gpr, acquisition, X=None, reference=None):
    """
    Plots slices along parameter coordinates for a series `X` of given points (the GPR
    training set if not specified). For each coordinate, there is a slice per point,
    leaving all coordinates of that point fixed except for the one being sliced.

    Lines are coloured according to the value of the mean GP at points X.

    # TODO: make acq func optional
    """
    params = list(model.parameterization.sampled_params())
    fig, axes = plt.subplots(
        nrows=2,
        ncols=len(params),
        sharex="col",
        layout="constrained",
        figsize=(4 * len(params), 4),
        dpi=200,
    )
    # Define X to plot
    if X is None:
        X = gpr.X_train.copy()
        y = gpr.y_train.copy()
    else:
        y = gpr.predict(X)
    min_y, max_y = min(y), max(y)
    norm_y = lambda y: (y - min_y) / (max_y - min_y)
    prior_bounds = model.prior.bounds(confidence_for_unbounded=0.999)
    Xs_for_plots = dict(
        (p, param_samples_for_slices(X, i, prior_bounds[i], n=200))
        for i, p in enumerate(params)
    )
    if reference is not None:
        reference = _prepare_reference(reference, model)
    cmap = matplotlib.colormaps["viridis"]
    for i, p in enumerate(params):
        for j, Xs_j in enumerate(Xs_for_plots[p]):
            cmap_norm = cmap(norm_y(y[j]))
            alpha = 1
            # TODO: could cut by half # of GP evals by reusing for acq func
            axes[0, i].plot(Xs_j[:, i], gpr.predict(Xs_j), c=cmap_norm, alpha=alpha)
            axes[0, i].scatter(X[j][i], y[j], color=cmap_norm, alpha=alpha)
            axes[0, i].set_ylabel(r"$\log(p)$")
            acq_values = acquisition(Xs_j, gpr)
            axes[1, i].plot(Xs_j[:, i], acq_values, c=cmap_norm, alpha=alpha)
            axes[1, i].set_ylabel(r"$\alpha(\mu,\sigma)$")
            label = model.parameterization.labels()[p]
            if label != p:
                label = "$" + label + "$"
            axes[1, i].set_xlabel(label)
            bounds = (reference or {}).get(p)
        if bounds is not None:
            for ax in axes[:, i]:
                if len(bounds) == 5:
                    ax.axvspan(
                        bounds[0], bounds[4], facecolor="tab:blue", alpha=0.2, zorder=-99
                    )
                    ax.axvspan(
                        bounds[1], bounds[3], facecolor="tab:blue", alpha=0.2, zorder=-99
                    )
                ax.axvline(bounds[2], c="tab:blue", alpha=0.3, ls="--")



def getdist_add_training(
    getdist_plot,
    model,
    gpr,
    colormap="viridis",
    marker=".",
    marker_inf="x",
    highlight_last=False,
):
    """
    Adds the training points to a GetDist triangle plot, coloured according to
    their log-posterior value.

    Parameters
    ----------
    getdist_plot : `GetDist triangle plot <https://getdist.readthedocs.io/en/latest/plots.html?highlight=triangle_plot#getdist.plots.GetDistPlotter.triangle_plot>`_
        Contains the marginalized contours and potentially other things.

    model : Cobaya model
        The model that was used to run the GP on

    gpr : GaussianProcessRegressor
        The trained GP Regressor containing the samples.

    colormap : matplotlib colormap, optional (default="viridis")
        Color map from which to get the color scale to represent the GP model value for
        the training points.

    marker : matplotlib marker, optional (default=".")
        Marker to be used for the training points.

    marker_inf : matplotlib marker, optional (default=".")
        Marker to be used for the non-finite training points.

    highlight_last: bool (default=False)
        Draw a red circle around the points added in the last iteration

    Returns
    -------
    The GetDist triangle plot with the added training points.
    """
    # Gather axes and bounds
    sampled_params = list(model.parameterization.sampled_params())
    d = len(sampled_params)
    ax_dict = {}
    bounds = [None] * len(sampled_params)
    for i, pi in enumerate(sampled_params):
        for j, pj in enumerate(sampled_params):
            ax = getdist_plot.get_axes_for_params(pi, pj, ordered=True)
            if not ax:
                continue
            ax_dict[(i, j)] = ax
            bounds[i] = ax.get_xlim()
            bounds[j] = ax.get_ylim()
    # Now reduce the set of points to the ones within ranges
    # (needed to get good limits for the colorbar of the log-posterior)
    Xs_finite = np.copy(gpr.X_train)
    ys_finite = np.copy(gpr.y_train)
    Xs_infinite = np.copy(gpr.X_train_infinite)
    for i, (mini, maxi) in enumerate(bounds):
        i_within_finite = np.argwhere(
            np.logical_and(mini < Xs_finite[:, i], Xs_finite[:, i] < maxi)
        )
        Xs_finite = np.atleast_2d(np.squeeze(Xs_finite[i_within_finite]))
        ys_finite = np.atleast_1d(np.squeeze(ys_finite[i_within_finite]))
        i_within_infinite = np.argwhere(
            np.logical_and(mini < Xs_infinite[:, i], Xs_infinite[:, i] < maxi)
        )
        Xs_infinite = np.atleast_2d(np.squeeze(Xs_infinite[i_within_infinite]))
        if highlight_last:
            Xs_last = gpr.last_appended[0]
            i_within_last = np.argwhere(
                np.logical_and(mini < Xs_last[:, i], Xs_last[:, i] < maxi)
            )
            X_last = np.atleast_2d(np.squeeze(Xs_last[i_within_last]))
    if len(Xs_finite) == 0 and len(Xs_infinite) == 0:  # no points within plotting ranges
        return
    # Create colormap with appropriate limits
    cmap = matplotlib.colormaps[colormap]
    if len(Xs_finite):
        Ncolors = 256
        color_bounds = np.linspace(min(ys_finite), max(ys_finite), Ncolors)
        norm = matplotlib.colors.BoundaryNorm(color_bounds, Ncolors)
    # Add points
    for (i, j), ax in ax_dict.items():
        if highlight_last and len(Xs_last) > 0:
            points_last = Xs_last[:, [i, j]]
            ax.scatter(
                *points_last.T,
                marker="o",
                c=len(points_last) * [[0, 0, 0, 0]],
                edgecolor="r",
                lw=0.5,
            )
        if len(Xs_finite) > 0:
            points_finite = Xs_finite[:, [i, j]]
            sc = ax.scatter(
                *points_finite.T, marker=marker, c=norm(ys_finite), alpha=0.3, cmap=cmap
            )
        if len(Xs_infinite) > 0:
            points_infinite = Xs_infinite[:, [i, j]]
            ax.scatter(*points_infinite.T, marker=marker_inf, s=20, c="k", alpha=0.3)
    # Colorbar
    if len(Xs_finite) > 0:
        getdist_plot.fig.colorbar(
            cm.ScalarMappable(norm=norm, cmap=cmap),
            label=r"$\log(p)$",
            ax=getdist_plot.fig.add_axes(
                [1 - 0.2 / d, 1 - 0.85 / d, 0.5 / d, 0.5 / d],
                frame_on=False,
                xticks=[],
                yticks=[],
            ),
            ticks=np.linspace(min(ys_finite), max(ys_finite), 5),
            location="left",
        )
    return getdist_plot


def plot_convergence(
    convergence_criterion,
    evaluations="total",
    marker="",
    axes=None,
    ax_labels=True,
    legend_loc="upper right",
):
    """
    Plots the value of the convergence criterion as function of the number of
    (accepted) training points.

    Parameters
    ----------
    convergence_criterion : The instance of the convergence criterion which has
        been called in the BO loop

    evaluations : "total" or "accepted"
        Whether to plot the total number of posterior evaluations or only the
        accepted steps.

    marker : matplotlib marker, optional (default="")
        Marker used for the plot. Will be passed to ``matplotlib.pyplot.plot``.

    axes : matplotlib axes, optional
        Axes to be used, if passed.

    ax_labels : bool, optional (default: True)
        Add axes labels.

    legend_loc : str (default: "upper right")
        Location of the legend.

    Returns
    -------
    The plot convergence criterion vs. number of training points
    """
    if not isinstance(convergence_criterion, Sequence):
        convergence_criterion = [convergence_criterion]
    if axes is None:
        fig, axes = plt.subplots()
    else:
        fig = axes.get_figure()
    for i, cc in enumerate(convergence_criterion):
        color = plt.rcParams["axes.prop_cycle"].by_key()["color"][i]
        values, n_posterior_evals, n_accepted_evals = cc.get_history()
        name = cc.__class__.__name__
        n_evals = np.array(
            {"total": n_posterior_evals, "accepted": n_accepted_evals}[evaluations],
            dtype=int,
        )
        try:
            axes.plot(n_evals, values, marker=marker, color=color, label=name)
        except KeyError as excpt:
            raise ValueError(
                "'evaluations' must be either 'total' or 'accepted'."
            ) from excpt
        if hasattr(cc, "limit"):
            axes.axhline(cc.limit, ls="--", lw="0.5", c=color)
    if ax_labels:
        axes.set_xlabel(f"{evaluations} number of posterior evaluations")
        axes.set_ylabel("Value of convergence criterion")
    axes.set_yscale("log")
    axes.grid(axis="y")
    axes.legend(loc=legend_loc)
    return fig, axes


def _prepare_reference(
    reference,
    model,
):
    """
    Turns `reference` into a dict with parameters as keys and a list of 5 numbers as
    values: two lower bounds, a central value, and two upper bounds, e.g. percentiles
    5, 25, 50, 75, 95.

    If getdist.MCSamples passed, bounds are by default 68% and 95%, and the central value
    is the mean.
    """
    # Ensure it is a dict
    try:
        from getdist import MCSamples  # pylint: disable=import-outside-toplevel
        if isinstance(reference, MCSamples):
            means = reference.getMeans()
            margstats = reference.getMargeStats()
            bounds = {}
            for p in model.parameterization.sampled_params():
                # NB: numerOfName doest not use renames; needs to find "original" name
                p_in_ref = reference.paramNames.parWithName(p).name
                i_p = reference.paramNames.numberOfName(p_in_ref)
                # by default lims/contours are [68, 95, 99]
                try:
                    lims = margstats.parWithName(p).limits
                except AttributeError as excpt:
                    raise ValueError(
                        f"Could not find parameter {p} in reference sample, which "
                        f"includes {reference.getParamNames().list()})"
                    ) from excpt
                bounds[p] = [
                    lims[1].lower,
                    lims[0].lower,
                    means[i_p],
                    lims[0].upper,
                    lims[1].upper,
                ]
            reference = bounds
    except ModuleNotFoundError:  # getdist not installed
        return None
    if not isinstance(reference, Mapping):
        # Assume parameters in order; check right number of them
        if len(reference) != model.prior.d():
            raise ValueError(
                "reference must be a list containing bounds per parameter for all of them"
                ", or a dict with parameters as keys and these same values."
            )
        reference = dict(zip(model.parameterization.sampled_params(), reference))
    # Ensure it contains all parameters and 5 numbers (or None's) per parameter
    for p in model.parameterization.sampled_params():
        if p not in reference:
            reference[p] = [None] * 5
        values = reference[p]
        if isinstance(values, Number):
            values = [values]
        if len(values) == 1:
            reference[p] = [None, None] + list(values) + [None, None]
        elif len(values) != 5:
            raise ValueError(
                "the elements of reference must be either a single central value, or a "
                "list of 5 elements: [lower_bound_2, lower_bound_1, central_value, "
                "upper_bound_2, upper_bound_1]."
            )
    return reference


def plot_points_distribution(
    model,
    gpr,
    convergence_criterion,
    progress,
    colormap="viridis",
    reference=None,
):
    """
    Plots the evolution of the run along true model evaluations, showing evolution of the
    convergence criterion and the values of the log-posterior and the individual
    parameters.

    Can take a reference sample or reference bounds (dict with parameters as keys and 5
    sorted bounds as values, or alternatively just a central value).
    """
    X = gpr.X_train_all
    y = gpr.y_train_all
    if gpr.infinities_classifier is not None:
        y_finite = gpr.infinities_classifier.y_finite
    else:
        y_finite = np.full(shape=len(y), fill_value=True)
    if reference is not None:
        reference = _prepare_reference(reference, model)
    fig, axes = plt.subplots(
        nrows=2 + model.prior.d(),
        ncols=1,
        sharex=True,
        figsize=(min(4, 0.3 * len(X)), 1.5 * (2 + X.shape[1])),
        dpi=400,
    )
    fig.set_tight_layout(True)
    i_eval = list(range(1, 1 + len(X)))
    # TOP: convergence plot
    try:
        plot_convergence(
            convergence_criterion,
            evaluations="total",
            marker="",
            axes=axes[0],
            ax_labels=False,
            legend_loc="upper left",
        )
    except ValueError:  # no criterion computed yet
        pass
    axes[0].set_ylabel("Conv. crit.")
    # 2nd: posterior plot
    kwargs_accepted = {
        "marker": ".",
        "linewidths": 0.1,
        "edgecolor": "0.1",
        "cmap": colormap,
    }
    axes[1].scatter(i_eval, y, c=np.where(y_finite, y, np.inf), **kwargs_accepted)
    axes[1].set_ylabel(r"$\log(p)$")
    axes[1].grid(axis="y")
    # NEXT: parameters plots
    for i, p in enumerate(model.parameterization.sampled_params()):
        label = model.parameterization.labels()[p]
        ax = axes[i + 2]
        if gpr.infinities_classifier is not None and sum(y_finite) < len(X):
            ax.scatter(
                i_eval,
                X[:, i],
                marker="x",
                c=np.where(y_finite, None, 0.5),
                cmap="gray",
                vmin=0,
                vmax=1,
                s=20,
            )
        ax.scatter(
            i_eval,
            X[:, i],
            c=np.where(y_finite, y, np.inf),
            **kwargs_accepted,
        )
        bounds = (reference or {}).get(p)
        if bounds is not None:
            if len(bounds) == 5:
                ax.axhspan(
                    bounds[0], bounds[4], facecolor="tab:blue", alpha=0.2, zorder=-99
                )
                ax.axhspan(
                    bounds[1], bounds[3], facecolor="tab:blue", alpha=0.2, zorder=-99
                )
            ax.axhline(bounds[2], c="tab:blue", alpha=0.3, ls="--")
        ax.set_ylabel("$" + label + "$" if label != p else p)
        ax.grid(axis="y")
    axes[0].set_xlim(0, len(X) + 0.5)
    axes[-1].set_xlabel("Number of posterior evaluations")
    n_train = progress.data["n_total"][1]
    for ax in axes:
        ax.axvspan(0, n_train + 0.5, facecolor="0.85", zorder=-999)
        for n_iteration in progress.data["n_total"][1:]:
            ax.axvline(n_iteration + 0.5, ls="--", c="0.75", zorder=-9)
    # TODO: make sure the x ticks are int


def plot_distance_distribution(
    points, mean, covmat, density=False, show_added=True, ax=None
):
    """
    Plots a histogram of the distribution of points with respect to the number of standard
    deviations. Confidence level boundaries (Gaussian approximantion, dimension-dependent)
    are shown too.

    Parameters
    ----------
    points: array-like, with shape ``(N_points, N_dimensions)``, or GPR instance
        Points to be used for the histogram.
    mean: array-like, ``(N_dimensions)``.
        Mean of the distribution.
    covmat: array-like, ``(N_dimensions, N_dimensions)``.
        Covariance matrix of the distribution.
    density: bool (default: False)
        If ``True``, bin height is normalised to the (hyper)volume of the (hyper)spherical
        shell corresponding to each standard deviation.
    show_added: bool (default True)
        Colours the stacks depending on how early or late the corresponding points were
        added (bluer stacks represent newer points).
    ax: matplotlib axes
        If provided, they will be used for the plot.

    Returns
    -------
    Tuple of current figure and axes ``(fig, ax)``.
    """
    if isinstance(points, GaussianProcessRegressor):
        points = points.X_train
    dim = np.atleast_2d(points).shape[1]
    radial_distances = gaussian_distance(points, mean, covmat)
    bins = list(range(0, int(np.ceil(np.max(radial_distances))) + 1))
    num_or_dens = "Density" if density else "Number"
    if density:
        volumes = [
            volume_sphere(bins[i], dim) - volume_sphere(bins[i - 1], dim)
            for i in range(1, len(bins))
        ]
        weights = [1 / volumes[int(np.floor(r))] for r in radial_distances]
    else:
        weights = np.ones(len(radial_distances))
    if ax is None:
        fig, ax = plt.subplots()
    else:
        fig = ax.get_figure()
    title_str = f"{num_or_dens} of points per standard deviation"
    if show_added:
        title_str += " (bluer=newer)"
        cmap = cm.get_cmap("Spectral")
        colors = [cmap(i / len(points)) for i in range(len(points))]
        ax.hist(
            np.atleast_2d(radial_distances),
            bins=bins,
            weights=np.atleast_2d(weights),
            color=colors,
            stacked=True,
        )
    else:
        ax.hist(radial_distances, bins=bins, weights=weights)
    ax.set_title(title_str)
    cls = [credibility_of_nstd(s, 1) for s in [1, 2, 3, 4]]  # using 1d cl's as reference
    nstds = [1, 2, 3, 4]
    linestyles = ["-", "--", "-.", ":"]
    for nstd, ls in zip(nstds, linestyles):
        std_of_cl = nstd_of_1d_nstd(nstd, dim)
        if std_of_cl < max(radial_distances):
            ax.axvline(
                std_of_cl,
                c="0.75",
                ls=ls,
                zorder=-99,
                label=f"${100 * credibility_of_nstd(std_of_cl, dim):.2f}\%$ prob mass",
            )
    ax.set_ylabel(f"{num_or_dens} of points")
    ax.set_xlabel("Number of standard deviations")
    ax.legend(loc="upper right")
    return (fig, ax)


def _plot_2d_model_acquisition(gpr, acquisition, last_points=None, res=200):
    """
    Contour plots for model prediction and acquisition function value of a 2d model.

    If ``last_points`` passed, they are highlighted.
    """
    if gpr.d != 2:
        warnings.warn("This plots are only possible in 2d.")
        return
    # TODO: option to restrict bounds to the min square containing traning samples,
    #       with some padding
    bounds = gpr.bounds
    x = np.linspace(bounds[0][0], bounds[0][1], res)
    y = np.linspace(bounds[1][0], bounds[1][1], res)
    X, Y = np.meshgrid(x, y)
    xx = np.ascontiguousarray(np.vstack([X.reshape(X.size), Y.reshape(Y.size)]).T)
    model_mean = gpr.predict(xx)
    # TODO: maybe change this one below if __call__ method added to GP_acquisition
    acq_value = acquisition(xx, gpr, eval_gradient=False)
    # maybe show the next max of acquisition
    acq_max = xx[np.argmax(acq_value)]
    fig, ax = plt.subplots(1, 2, figsize=(8, 4))
    cmap = [cm.magma, cm.viridis]
    label = ["Model mean (log-posterior)", "Acquisition function value"]
    for i, Z in enumerate([model_mean, acq_value]):
        ax[i].set_title(label[i])
        # Boost the upper limit to avoid truncation errors.
        Z = np.clip(Z, min(Z[np.isfinite(Z)]), max(Z[np.isfinite(Z)]))
        levels = np.arange(min(Z) * 0.99, max(Z) * 1.01, (max(Z) - min(Z)) / 500)
        Z = Z.reshape(*X.shape)
        norm = cm.colors.Normalize(vmax=Z.max(), vmin=Z.min())
        # # Background of the same color as the bottom of the colormap, to avoid "gaps"
        # plt.gca().set_facecolor(cmap[i].colors[0])
        ax[i].contourf(X, Y, Z, levels, cmap=cm.get_cmap(cmap[i], 256), norm=norm)
        points = ax[i].scatter(
            *gpr.X_train.T, edgecolors="deepskyblue", marker=r"$\bigcirc$"
        )
        # Plot position of next best sample
        point_max = ax[i].scatter(*acq_max, marker="x", color="k")
        if last_points is not None:
            points_last = ax[i].scatter(
                *last_points.T, edgecolors="violet", marker=r"$\bigcirc$"
            )
        # Bounds
        ax[i].set_xlim(bounds[0][0], bounds[0][1])
        ax[i].set_ylim(bounds[1][0], bounds[1][1])
        # Remove ticks, for ilustrative purposes only
        # ax[i].set_xticks([], minor=[])
        # ax[i].set_yticks([], minor=[])
    legend_labels = {points: "Training points"}
    if last_points is not None:
        legend_labels[points_last] = "Points added in last iteration."
    legend_labels[point_max] = "Next optimal location"
    fig.legend(
        list(legend_labels), list(legend_labels.values()), loc="lower center", ncol=99
    )
    plt.subplots_adjust(left=0.1, right=0.9, bottom=0.15)


def _plot_2d_model_acquisition_finite(gpr, acquisition, last_points=None, res=200):
    """
    Contour plots for model prediction and acquisition function value of a 2d model.

    If ``last_points`` passed, they are highlighted.
    """
    if gpr.d != 2:
        warnings.warn("This plots are only possible in 2d.")
        return
    # TODO: option to restrict bounds to the min square containing traning samples,
    #       with some padding
    bounds = gpr.bounds
    x = np.linspace(bounds[0][0], bounds[0][1], res)
    y = np.linspace(bounds[1][0], bounds[1][1], res)
    X, Y = np.meshgrid(x, y)
    xx = np.ascontiguousarray(np.vstack([X.reshape(X.size), Y.reshape(Y.size)]).T)
    model_mean = gpr.predict(xx)
    # TODO: maybe change this one below if __call__ method added to GP_acquisition
    acq_value = acquisition(xx, gpr, eval_gradient=False)
    # maybe show the next max of acquisition
    acq_max = xx[np.argmax(acq_value)]
    fig, ax = plt.subplots(1, 2, figsize=(8, 4))
    cmap = [cm.magma, cm.viridis]
    label = ["Model mean (log-posterior)", "Acquisition function value"]
    for i, Z in enumerate([model_mean, acq_value]):
        ax[i].set_title(label[i])
        # Boost the upper limit to avoid truncation errors.
        Z_finite = Z[np.isfinite(Z)]
        # Z_clipped = np.clip(Z_finite, min(Z[np.isfinite(Z)]), max(Z[np.isfinite(Z)]))
        Z_sort = np.sort(Z_finite)[::-1]
        top_x_perc = np.sort(Z_finite)[::-1][: int(len(Z_finite) * 0.5)]
        relevant_range = max(top_x_perc) - min(top_x_perc)
        levels = np.linspace(
            max(Z_finite) - 1.99 * relevant_range,
            max(Z_finite) + 0.01 * relevant_range,
            500,
        )
        Z[np.isfinite(Z)] = np.clip(Z_finite, min(levels), max(levels))
        Z = Z.reshape(*X.shape)
        norm = cm.colors.Normalize(vmax=max(levels), vmin=min(levels))
        ax[i].set_facecolor("grey")
        # # Background of the same color as the bottom of the colormap, to avoid "gaps"
        # plt.gca().set_facecolor(cmap[i].colors[0])
        ax[i].contourf(X, Y, Z, levels, cmap=cm.get_cmap(cmap[i], 256), norm=norm)
        points = ax[i].scatter(
            *gpr.X_train.T, edgecolors="deepskyblue", marker=r"$\bigcirc$"
        )
        # Plot position of next best sample
        point_max = ax[i].scatter(*acq_max, marker="x", color="k")
        if last_points is not None:
            points_last = ax[i].scatter(
                *last_points.T, edgecolors="violet", marker=r"$\bigcirc$"
            )
        # Bounds
        ax[i].set_xlim(bounds[0][0], bounds[0][1])
        ax[i].set_ylim(bounds[1][0], bounds[1][1])
        # Remove ticks, for ilustrative purposes only
        # ax[i].set_xticks([], minor=[])
        # ax[i].set_yticks([], minor=[])
    legend_labels = {points: "Training points"}
    if last_points is not None:
        legend_labels[points_last] = "Points added in last iteration."
    legend_labels[point_max] = "Next optimal location"
    fig.legend(
        list(legend_labels), list(legend_labels.values()), loc="lower center", ncol=99
    )
    plt.subplots_adjust(left=0.1, right=0.9, bottom=0.15)


def _plot_2d_model_acquisition_std(gpr, acquisition, last_points=None, res=200):
    """
    Contour plots for model prediction and acquisition function value of a 2d model.

    If ``last_points`` passed, they are highlighted.
    """
    if gpr.d != 2:
        warnings.warn("This plots are only possible in 2d.")
        return
    # TODO: option to restrict bounds to the min square containing traning samples,
    #       with some padding
    bounds = gpr.bounds
    x = np.linspace(bounds[0][0], bounds[0][1], res)
    y = np.linspace(bounds[1][0], bounds[1][1], res)
    X, Y = np.meshgrid(x, y)
    xx = np.ascontiguousarray(np.vstack([X.reshape(X.size), Y.reshape(Y.size)]).T)
    model_mean, model_std = gpr.predict(xx, return_std=True)
    # TODO: maybe change this one below if __call__ method added to GP_acquisition
    acq_value = acquisition(xx, gpr, eval_gradient=False)
    # maybe show the next max of acquisition
    acq_max = xx[np.argmax(acq_value)]
    fig, ax = plt.subplots(1, 3, figsize=(12, 4))
    cmap = [cm.magma, cm.viridis, cm.magma]
    label = ["Model mean (log-posterior)", "Acquisition function value", "Model std dev."]
    for i, Z in enumerate([model_mean, acq_value]):
        ax[i].set_title(label[i])
        # Boost the upper limit to avoid truncation errors.
        Z_finite = Z[np.isfinite(Z)]
        # Z_clipped = np.clip(Z_finite, min(Z[np.isfinite(Z)]), max(Z[np.isfinite(Z)]))
        Z_sort = np.sort(Z_finite)[::-1]
        top_x_perc = np.sort(Z_finite)[::-1][: int(len(Z_finite) * 0.5)]
        relevant_range = max(top_x_perc) - min(top_x_perc)
        levels = np.linspace(
            max(Z_finite) - 1.99 * relevant_range,
            max(Z_finite) + 0.01 * relevant_range,
            500,
        )
        Z[np.isfinite(Z)] = np.clip(Z_finite, min(levels), max(levels))
        Z = Z.reshape(*X.shape)
        norm = cm.colors.Normalize(vmax=max(levels), vmin=min(levels))
        ax[i].set_facecolor("grey")
        # # Background of the same color as the bottom of the colormap, to avoid "gaps"
        # plt.gca().set_facecolor(cmap[i].colors[0])
        ax[i].contourf(X, Y, Z, levels, cmap=cm.get_cmap(cmap[i], 256), norm=norm)
        points = ax[i].scatter(
            *gpr.X_train.T, edgecolors="deepskyblue", marker=r"$\bigcirc$"
        )
        # Plot position of next best sample
        point_max = ax[i].scatter(*acq_max, marker="x", color="k")
        if last_points is not None:
            points_last = ax[i].scatter(
                *last_points.T, edgecolors="violet", marker=r"$\bigcirc$"
            )
        # Bounds
        ax[i].set_xlim(bounds[0][0], bounds[0][1])
        ax[i].set_ylim(bounds[1][0], bounds[1][1])
        # Remove ticks, for ilustrative purposes only
        # ax[i].set_xticks([], minor=[])
        # ax[i].set_yticks([], minor=[])
    ax[2].set_title(label[2])
    Z = model_std
    Z_finite = Z[np.isfinite(model_mean)]
    Z[~np.isfinite(model_mean)] = -np.inf
    minz = min(Z_finite)
    zrange = max(Z_finite) - minz
    levels = np.linspace(minz, minz + (zrange if zrange > 0 else 0.00001), 500)
    # Z[np.isfinite(model_mean)] = np.clip(Z_finite, min(levels), max(levels))
    Z = Z.reshape(*X.shape)
    norm = cm.colors.Normalize(vmax=max(levels), vmin=min(levels))
    ax[2].set_facecolor("grey")
    ax[2].contourf(X, Y, Z, levels, cmap=cm.get_cmap(cmap[2], 256), norm=norm)
    points = ax[2].scatter(*gpr.X_train.T, edgecolors="deepskyblue", marker=r"$\bigcirc$")
    # Plot position of next best sample
    point_max = ax[2].scatter(*acq_max, marker="x", color="k")
    if last_points is not None:
        points_last = ax[2].scatter(
            *last_points.T, edgecolors="violet", marker=r"$\bigcirc$"
        )
    # Bounds
    ax[2].set_xlim(bounds[0][0], bounds[0][1])
    ax[2].set_ylim(bounds[1][0], bounds[1][1])
    legend_labels = {points: "Training points"}
    if last_points is not None:
        legend_labels[points_last] = "Points added in last iteration."
    legend_labels[point_max] = "Next optimal location"
    fig.legend(
        list(legend_labels), list(legend_labels.values()), loc="lower center", ncol=99
    )
    plt.subplots_adjust(left=0.1, right=0.9, bottom=0.15)
