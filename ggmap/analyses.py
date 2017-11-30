import tempfile
import shutil
import subprocess
import sys
import hashlib
import os
import pickle
from io import StringIO
import collections
import datetime
import time
import numpy as np
import json

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

from skbio.stats.distance import DistanceMatrix
from skbio.tree import TreeNode

from ggmap.snippets import (pandas2biom, cluster_run, biom2pandas)


plt.switch_backend('Agg')
plt.rc('font', family='DejaVu Sans')

FILE_REFERENCE_TREE = None
QIIME_ENV = 'qiime_env'
QIIME2_ENV = 'qiime2-2017.10'


def _get_ref_phylogeny(file_tree=None, env=QIIME_ENV):
    """Use QIIME config to infer location of reference tree or pass given tree.

    Parameters
    ----------
    file_tree : str
        Default: None.
        If None is set, than we need to activate qiime environment, print
        config and search for the rigth path information.
        Otherwise, specified reference tree is returned without doing anything.
    env : str
        Default: global constant QIIME_ENV value.
        Conda environment name for QIIME.

    Returns
    -------
    Filepath to reference tree.
    """
    global FILE_REFERENCE_TREE
    if file_tree is not None:
        return file_tree
    if FILE_REFERENCE_TREE is None:
        err = StringIO()
        with subprocess.Popen(("source activate %s && "
                               "print_qiime_config.py "
                               "| grep 'pick_otus_reference_seqs_fp:'" %
                               env),
                              shell=True,
                              stdout=subprocess.PIPE,
                              executable="bash") as call_x:
            out, err = call_x.communicate()
            if (call_x.wait() != 0):
                raise ValueError("_get_ref_phylogeny(): something went wrong")

            # convert from b'' to string
            out = out.decode()
            # split key:\tvalue
            out = out.split('\t')[1]
            # remove trailing \n
            out = out.rstrip()
            # chop '/rep_set/97_otus.fasta' from found path
            out = '/'.join(out.split('/')[:-2])
            FILE_REFERENCE_TREE = out + '/trees/97_otus.tree'
    return FILE_REFERENCE_TREE


def _getremaining(counts_sums):
    """Compute number of samples that have at least X read counts.

    Parameters
    ----------
    counts_sum : Pandas.Series
        Reads per sample.

    Returns
    -------
    Pandas.Series:
        Index = sequencing depths,
        Values = number samples with at least this sequencing depth.
    """
    d = dict()
    remaining = counts_sums.shape[0]
    numdepths = counts_sums.value_counts().sort_index()
    for depth, numsamples in numdepths.iteritems():
        d[depth] = remaining
        remaining -= numsamples
    return pd.Series(data=d, name='remaining').to_frame()


def _parse_alpha_div_collated(filename, metric=None):
    """Parse QIIME's alpha_div_collated file for plotting with matplotlib.

    Parameters
    ----------
    filename : str
        Filename of the alpha_div_collated file to be parsed. It is the result
        of QIIME's collate_alpha.py script.
    metric : str
        Provide the alpha diversity metric name, used to create the input file.
        Default is None, i.e. the metric name is guessed from the filename.

    Returns
    -------
    Pandas.DataFrame with the averaged (over all iterations) alpha diversities
    per rarefaction depth per sample.

    Raises
    ------
    IOError
        If the file cannot be read.
    """
    try:
        # read qiime's alpha div collated file. It is tab separated and nan
        # values come as 'n/a'
        x = pd.read_csv(filename, sep='\t', na_values=['n/a'])

        # make a two level index
        x.set_index(keys=['sequences per sample', 'iteration'], inplace=True)

        # remove the column that reports the single rarefaction files,
        # because it would otherwise become another sample
        del x['Unnamed: 0']

        # average over all X iterations
        x = x.groupby(['sequences per sample']).mean()

        # change pandas format of data for easy plotting
        x = x.stack().to_frame().reset_index()

        # guess metric name from filename
        if metric is None:
            metric = filename.split('/')[-1].split('.')[0]

        # give columns more appropriate names
        x = x.rename(columns={'sequences per sample': 'rarefaction depth',
                              'level_1': 'sample_name',
                              0: metric})

        # if there is only one (rarefaction) iteration, stacking results in a
        # slightly different DataFrame, which we are going to normalize here.
        if 'level_0' in x.columns:
            x['level_0'] = None
            x = x.rename(columns={'level_0': 'rarefaction depth'})

        return x
    except IOError:
        raise IOError('Cannot read file "%s"' % filename)


def _plot_rarefaction_curves(data):
    """Plot rarefaction curves along with loosing sample stats + read count
       histogram.

    Parameters
    ----------
    data : dict()
        The result of rarefaction_curves(), i.e. a dict with the three keys
        - metrics
        - remaining
        - readcounts

    Returns
    -------
    Matplotlib figure
    """
    fig, axes = plt.subplots(2+len(data['metrics']),
                             1,
                             figsize=(5, (2+len(data['metrics']))*5),
                             sharex=False)

    # read count histogram
    ax = axes[0]
    n, bins, patches = ax.hist(data['readcounts'].fillna(0.0),
                               50,
                               facecolor='black',
                               alpha=0.75)
    ax.set_title('Read count distribution across samples')
    ax.set_xlabel("read counts")
    ax.set_ylabel("# samples")
    ax.get_xaxis().set_major_formatter(
        FuncFormatter(lambda x, p: format(int(x), ',')))

    # loosing samples
    ax = axes[1]
    x = data['remaining']
    x['lost'] = data['readcounts'].shape[0] - x['remaining']
    x.index.name = 'readcounts'
    ax.plot(x.index, x['remaining'], label='remaining')
    ax.plot(x.index, x['lost'], label='lost')
    ax.set_xlabel("rarefaction depth")
    ax.set_ylabel("# samples")
    ax.set_title('How many of the %i samples do we loose?' %
                 data['readcounts'].shape[0])
    ax.get_xaxis().set_major_formatter(
        FuncFormatter(lambda x, p: format(int(x), ',')))
    lostHalf = abs(x['remaining'] - x['lost'])
    lostHalf = lostHalf[lostHalf == lostHalf.min()].index[0]
    ax.set_xlim(0, lostHalf * 1.1)
    # p = ax.set_xscale("log", nonposx='clip')

    for i, metric in enumerate(data['metrics'].keys()):
        for sample, g in data['metrics'][metric].groupby('sample_name'):
            axes[i+2].errorbar(g['rarefaction depth'], g[g.columns[-1]])
        axes[i+2].set_ylabel(g.columns[-1])
        axes[i+2].set_xlabel('rarefaction depth')
        axes[i+2].set_xlim(0, lostHalf * 1.1)
        axes[i+2].get_xaxis().set_major_formatter(
            FuncFormatter(lambda x, p: format(int(x), ',')))

    return fig


def rarefaction_curves(counts,
                       metrics=["PD_whole_tree", "shannon", "observed_otus"],
                       num_steps=20, reference_tree=None, max_depth=None,
                       num_iterations=10, **executor_args):
    """Produce rarefaction curves, i.e. reads/sample and alpha vs. depth plots.

    Parameters
    ----------
    counts : Pandas.DataFrame
        The raw read counts. Columns are samples, rows are features.
    metrics : [str]
        List of alpha diversity metrics to use.
        Default is ["PD_whole_tree", "shannon", "observed_otus"]
    num_steps : int
        Number of different rarefaction steps to test. The higher the slower.
        Default is 20.
    reference_tree : str
        Filepath to a newick tree file, which will be the phylogeny for unifrac
        alpha diversity distances. By default, qiime's GreenGenes tree is used.
    max_depth : int
        Maximal rarefaction depth. By default counts.sum().describe()['75%'] is
        used.
    num_iterations : int
        Default: 10.
        Number of iterations to rarefy the input table.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    plt figure
    """
    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

    def commands(workdir, ppn, args):
        max_rare_depth = args['counts'].sum().describe()['75%']
        if args['max_depth'] is not None:
            max_rare_depth = args['max_depth']
        commands = []

        # Alpha rarefaction command
        commands.append(('parallel_multiple_rarefactions.py '
                         '-T '
                         '-i %s '      # Input filepath, (the otu table)
                         '-m %i '      # Min seqs/sample
                         '-x %i '      # Max seqs/sample (inclusive)
                         '-s %i '      # Levels: min, min+step... for level
                                       # <= max
                         '-o %s '      # Write output rarefied otu tables here
                                       # makes dir if it doesn’t exist
                         '-n %i '      # number iterations per depth
                         '--jobs_to_start %i') % (  # Number of jobs to start
            workdir+'/input.biom',
            max(1000, args['counts'].sum().min()),
            max_rare_depth,
            (max_rare_depth - args['counts'].sum().min())/args['num_steps'],
            workdir+'/rare/rarefaction/',
            num_iterations,
            ppn))

        # Alpha diversity on rarefied OTU tables command
        commands.append(('parallel_alpha_diversity.py '
                         '-T '
                         '-i %s '         # Input path, must be directory
                         '-o %s '         # Output path, must be directory
                         '--metrics %s '  # Metrics to use, comma delimited
                         '-t %s '         # Path to newick tree file, required
                                          # for phylogenetic metrics
                         '--jobs_to_start %i') % (  # Number of jobs to start
            workdir+'/rare/rarefaction/',
            workdir+'/rare/alpha_div/',
            ",".join(args['metrics']),
            _get_ref_phylogeny(reference_tree),
            ppn))

        # Collate alpha command
        commands.append(('collate_alpha.py '
                         '-i %s '      # Input path (a directory)
                         '-o %s') % (  # Output path (a directory).
                                       # will be created if needed
            workdir+'/rare/alpha_div/',
            workdir+'/rare/alpha_div_collated/'))

        return commands

    def post_execute(workdir, args):
        sums = args['counts'].sum()
        results = {'metrics': dict(),
                   'remaining': _getremaining(sums),
                   'readcounts': sums}
        for metric in args['metrics']:
            results['metrics'][metric] = _parse_alpha_div_collated(
                workdir+'/rare/alpha_div_collated/'+metric+'.txt')

        return results

    def post_cache(cache_results):
        cache_results['results'] = \
            _plot_rarefaction_curves(cache_results['results'])
        return cache_results

    if reference_tree is not None:
        reference_tree = os.path.abspath(reference_tree)
    return _executor('rare',
                     {'counts': counts,
                      'metrics': metrics,
                      'num_steps': num_steps,
                      'max_depth': max_depth,
                      'num_iterations': num_iterations,
                      'reference_tree': reference_tree},
                     pre_execute,
                     commands,
                     post_execute,
                     post_cache,
                     **executor_args)


def rarefy(counts, rarefaction_depth,
           ppn=1,
           **executor_args):
    """Rarefies a given OTU table to a given depth. This depth should be
       determined by looking at rarefaction curves.

    Paramaters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    rarefaction_depth : int
        Rarefaction depth that must be applied to counts.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    Pandas.DataFrame: Rarefied OTU table."""

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])

    def commands(workdir, ppn, args):
        commands = []
        commands.append(('multiple_rarefactions.py '
                         '-i %s '                    # input biom file
                         '-m %i '                    # min rarefaction depth
                         '-x %i '                    # max rarefaction depth
                         '-s 1 '                     # depth steps
                         '-o %s '                    # output directory
                         '-n 1 '                  # number iterations per depth
                         ) % (   # number parallel jobs
            workdir+'/input.biom',
            args['rarefaction_depth'],
            args['rarefaction_depth'],
            workdir+'/rarefactions'))

        return commands

    def post_execute(workdir, args):
        return biom2pandas(workdir+'/rarefactions/rarefaction_%i_0.biom' %
                           args['rarefaction_depth'])

    return _executor('rarefy',
                     {'counts': counts,
                      'rarefaction_depth': rarefaction_depth},
                     pre_execute,
                     commands,
                     post_execute,
                     ppn=ppn,
                     **executor_args)


def alpha_diversity(counts, rarefaction_depth,
                    metrics=["PD_whole_tree", "shannon", "observed_otus"],
                    num_iterations=10, reference_tree=None,
                    **executor_args):
    """Computes alpha diversity values for given BIOM table.

    Paramaters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    rarefaction_depth : int
        Rarefaction depth that must be applied to counts.
    metrics : [str]
        Alpha diversity metrics to be computed.
    num_iterations : int
        Number of iterations to rarefy the input table.
    reference_tree : str
        Reference tree file name for phylogenetic metics like unifrac.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    Pandas.DataFrame: alpha diversity values for each sample (rows) for every
    chosen metric (columns)."""

    def update_metric(metric):
        if metric == 'PD_whole_tree':
            return 'faith_pd'
        return metric

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])
        os.mkdir(workdir+'/rarefaction/')
        os.mkdir(workdir+'/alpha/')
        os.mkdir(workdir+'/alpha_plain/')
        # copy reference tree and correct missing branch lengths
        if len(set(args['metrics']) &
               set(['PD_whole_tree'])) > 0:
            tree_ref = TreeNode.read(
                _get_ref_phylogeny(args['reference_tree']))
            for node in tree_ref.preorder():
                if node.length is None:
                    node.length = 0
            tree_ref.write(workdir+'/reference.tree')

    def commands(workdir, ppn, args):
        commands = []

        commands.append(
            ('qiime tools import '
             '--input-path %s '
             '--type "FeatureTable[Frequency]" '
             '--source-format BIOMV210Format '
             '--output-path %s ') %
            (workdir+'/input.biom', workdir+'/input'))
        if 'PD_whole_tree' in args['metrics']:
            commands.append(
                ('qiime tools import '
                 '--input-path %s '
                 '--output-path %s '
                 '--type "Phylogeny[Rooted]"') %
                (workdir+'/reference.tree',
                 workdir+'/reference_tree.qza'))

        iterations = range(args['num_iterations'])
        if args['rarefaction_depth'] is None:
            iterations = [0]
        for iteration in iterations:
            file_raretable = workdir+'/rarefaction/rare_%s_%i.qza' % (
                args['rarefaction_depth'], iteration)
            if args['rarefaction_depth'] is not None:
                commands.append(
                    ('qiime feature-table rarefy '
                     '--i-table %s '
                     '--p-sampling-depth %i '
                     '--o-rarefied-table %s') %
                    (workdir+'/input.qza', args['rarefaction_depth'],
                     file_raretable)
                )
            else:
                commands.append('cp %s %s' % (
                    workdir+'/input.qza',
                    workdir+'/rarefaction/rare_%s_%i.qza' % (
                        rarefaction_depth, iteration)))
            for metric in args['metrics']:
                file_alpha = workdir+'/alpha/alpha_%s_%i_%s.qza' % (
                    args['rarefaction_depth'], iteration, metric)
                plugin = 'alpha'
                treeinput = ''
                if metric == 'PD_whole_tree':
                    plugin = 'alpha-phylogenetic'
                    treeinput = '--i-phylogeny %s' % (
                        workdir+'/reference_tree.qza')
                commands.append(
                    ('qiime diversity %s '
                     '--i-table %s '
                     '--p-metric %s '
                     ' %s '
                     '--o-alpha-diversity %s') %
                    (plugin, file_raretable,
                     update_metric(metric),
                     treeinput,
                     file_alpha))
                commands.append(
                    ('qiime tools export '
                     '%s/alpha/alpha_%s_%i_%s.qza '
                     '--output-dir %s/alpha_plain/%s/%i/%s') %
                    (workdir, args['rarefaction_depth'], iteration, metric,
                     workdir, args['rarefaction_depth'], iteration, metric))

        return commands

    def post_execute(workdir, args):
        dir_plain = '%s/alpha_plain/%s/' % (workdir, args['rarefaction_depth'])
        results_alpha = dict()
        for iteration in next(os.walk(dir_plain))[1]:
            for metric in next(os.walk(dir_plain + '/' + iteration))[1]:
                if metric not in results_alpha:
                    results_alpha[metric] = []
                file_alpha = '%s/%s/%s/alpha-diversity.tsv' % (
                    dir_plain, iteration, metric)
                results_alpha[metric].append(
                    pd.read_csv(file_alpha, sep="\t", index_col=0))
        for metric in results_alpha.keys():
            results_alpha[metric] = pd.concat(
                results_alpha[metric], axis=1).mean(axis=1)
            results_alpha[metric].name = metric
        result = pd.concat(results_alpha.values(), axis=1)
        result.index.name = 'iter%s_depth%s' % (
            args['num_iterations'], args['rarefaction_depth'])
        return result

    if reference_tree is not None:
        reference_tree = os.path.abspath(reference_tree)
    return _executor('adiv',
                     {'counts': counts,
                      'metrics': metrics,
                      'rarefaction_depth': rarefaction_depth,
                      'num_iterations': num_iterations,
                      'reference_tree': reference_tree},
                     pre_execute,
                     commands,
                     post_execute,
                     environment=QIIME2_ENV,
                     ppn=1,
                     **executor_args)


def beta_diversity(counts,
                   metrics=["unweighted_unifrac",
                            "weighted_unifrac",
                            "bray_curtis"],
                   reference_tree=None,
                   **executor_args):
    """Computes beta diversity values for given BIOM table.

    Parameters
    ----------
    counts : Pandas.DataFrame
        OTU counts
    metrics : [str]
        Beta diversity metrics to be computed.
    reference_tree : str
        Reference tree file name for phylogenetic metics like unifrac.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    Dict of Pandas.DataFrame, one per metric."""

    def update_metric(metric):
        if metric == 'bray_curtis':
            return 'braycurtis'
        elif metric == 'weighted_unifrac':
            return 'weighted_normalized_unifrac'
        return metric

    def pre_execute(workdir, args):
        # store counts as a biom file
        pandas2biom(workdir+'/input.biom', args['counts'])
        os.mkdir(workdir+'/beta_qza')
        # copy reference tree and correct missing branch lengths
        if len(set(args['metrics']) &
               set(['unweighted_unifrac', 'weighted_unifrac'])) > 0:
            tree_ref = TreeNode.read(
                _get_ref_phylogeny(args['reference_tree']))
            for node in tree_ref.preorder():
                if node.length is None:
                    node.length = 0
            tree_ref.write(workdir+'/reference.tree')

    def commands(workdir, ppn, args):
        metrics_phylo = []
        metrics_nonphylo = []
        for metric in map(update_metric, args['metrics']):
            if metric.endswith('_unifrac'):
                metrics_phylo.append(metric)
            else:
                metrics_nonphylo.append(metric)

        commands = []
        # import biom table into q2 fragment
        # commands.append('mkdir -p %s' % (workdir+'/beta_qza'))
        commands.append(
            ('qiime tools import '
             '--input-path %s '
             '--type "FeatureTable[Frequency]" '
             '--source-format BIOMV210Format '
             '--output-path %s ') %
            (workdir+'/input.biom', workdir+'/input'))
        for metric in metrics_nonphylo:
            commands.append(
                ('qiime diversity beta '
                 '--i-table %s '
                 '--p-metric %s '
                 '--p-n-jobs %i '
                 '--o-distance-matrix %s%s ') %
                (workdir+'/input.qza', metric, ppn,
                 workdir+'/beta_qza/', metric))
        for i, metric in enumerate(metrics_phylo):
            if i == 0:
                commands.append(
                    ('qiime tools import '
                     '--input-path %s '
                     '--output-path %s '
                     '--type "Phylogeny[Rooted]"') %
                    (workdir+'/reference.tree',
                     workdir+'/reference_tree.qza'))
            commands.append(
                ('qiime diversity beta-phylogenetic-alt '
                 '--i-table %s '
                 '--i-phylogeny %s '
                 '--p-metric %s '
                 '--p-n-jobs %i '
                 '--o-distance-matrix %s%s ') %
                (workdir+'/input.qza', workdir+'/reference_tree.qza',
                 metric,
                 # bug in q2 plugin: crashs 'if the number of threads requested
                 # exceeds the approximately n / 2 samples, then an exception
                 # is raised'
                 min(ppn, int(args['counts'].shape[1] / 2.2)),
                 workdir+'/beta_qza/', metric))
        for metric in metrics_nonphylo + metrics_phylo:
            commands.append(
                ('qiime tools export '
                 '%s/beta_qza/%s.qza '
                 '--output-dir %s/beta/%s/') %
                (workdir, metric, workdir, metric))
        return commands

    def post_execute(workdir, args):
        results = dict()
        for metric in args['metrics']:
            results[metric] = DistanceMatrix.read(
                '%s/beta/%s/distance-matrix.tsv' % (
                    workdir,
                    update_metric(metric)))
        return results

    if reference_tree is not None:
        reference_tree = os.path.abspath(reference_tree)
    return _executor('bdiv',
                     {'counts': counts,
                      'metrics': metrics,
                      'reference_tree': reference_tree},
                     pre_execute,
                     commands,
                     post_execute,
                     environment=QIIME2_ENV,
                     **executor_args)


def sepp(counts, chunksize=10000,
         reference_phylogeny=None, reference_alignment=None,
         reference_taxonomy=None,
         ppn=20, pmem='8GB', walltime='12:00:00', environment=QIIME2_ENV,
         **executor_args):
    """Tip insertion of deblur sequences into GreenGenes backbone tree.

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    reference_phylogeny : str
        Default: None.
        Filepath to a qza "Phylogeny[Rooted]" artifact, holding an alternative
        reference phylogeny for SEPP.
    reference_alignment : str
        Default: None.
        Filepath to a qza "FeatureData[AlignedSequence]" artifact, holding an
        alternative reference alignment for SEPP.
    reference_taxonomy : str
        Default: None.
        Filepath to a qza "FeatureData[Taxonomy]" artifact, holding an
        alternative reference taxonomy for SEPP.
    chunksize: int
        Default: 10000
        SEPP jobs seem to fail if too many sequences are submitted per job.
        Therefore, we can split your sequences in chunks of chunksize.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    ???"""
    def pre_execute(workdir, args):
        chunks = range(0, seqs.shape[0], args['chunksize'])
        for chunk, i in enumerate(chunks):
            # write all deblur sequences into one file per chunk
            file_fragments = workdir + '/sequences%s.mfa' % (chunk + 1)
            f = open(file_fragments, 'w')
            chunk_seqs = seqs.iloc[i:i + args['chunksize']]
            for header, sequence in chunk_seqs.iteritems():
                f.write('>%s\n%s\n' % (header, sequence))
            f.close()

    def commands(workdir, ppn, args):
        commands = []

        # import fasta sequences into qza
        commands.append(
            ('qiime tools import '
             '--input-path %s '
             '--output-path %s/rep-seqs${PBS_ARRAYID} '
             '--type "FeatureData[Sequence]"') %
            (workdir + '/sequences${PBS_ARRAYID}.mfa', workdir))

        ref_phylogeny = ""
        if reference_phylogeny is not None:
            ref_phylogeny = ' --i-reference-phylogeny %s ' % (
                reference_phylogeny)
        ref_alignment = ""
        if reference_alignment is not None:
            ref_alignment = ' --i-reference-alignment %s ' % (
                reference_alignment)

        commands.append(
            ('qiime fragment-insertion sepp '
             '--i-representative-sequences %s/rep-seqs${PBS_ARRAYID}.qza '
             '--p-threads %i '
             '%s%s'
             '--output-dir %s/res_${PBS_ARRAYID}') %
            (workdir, ppn, ref_phylogeny, ref_alignment, workdir))

        # export the placements
        commands.append(
            ('qiime tools export '
             '%s/res_${PBS_ARRAYID}/placements.qza '
             '--output-dir %s/res_${PBS_ARRAYID}/') %
            (workdir, workdir))

        # compute taxonomy from resulting tree and placements
        ref_taxonomy = ""
        if args['reference_taxonomy'] is not None:
            ref_taxonomy = \
                " --i-reference-taxonomy %s " % args['reference_taxonomy']
        commands.append(
            ('qiime fragment-insertion classify-otus-experimental '
             '--i-representative-sequences %s/rep-seqs${PBS_ARRAYID}.qza '
             '--i-tree %s/res_${PBS_ARRAYID}/tree.qza '
             '%s'
             '--o-classification %s/res_taxonomy_${PBS_ARRAYID}') %
            (workdir, workdir, ref_taxonomy, workdir))

        # export taxonomy to tsv file
        commands.append(
            ('qiime tools export '
             '%s/res_taxonomy_${PBS_ARRAYID}.qza '
             '--output-dir %s/res_taxonomy_${PBS_ARRAYID}/') %
            (workdir, workdir))

        # move taxonomy tsv to basedir
        commands.append(
            ('mv '
             '%s/res_taxonomy_${PBS_ARRAYID}/taxonomy.tsv '
             '%s/taxonomy_${PBS_ARRAYID}.tsv') %
            (workdir, workdir))

        return commands

    def post_execute(workdir, args):
        use_grid = executor_args['use_grid'] \
            if 'use_grid' in executor_args else True
        dry = executor_args['dry'] if 'dry' in executor_args else True

        files_placement = []
        for d in next(os.walk(workdir))[1]:
            if d.startswith('res_'):
                for f in next(os.walk(workdir+'/'+d))[2]:
                    if f == 'placements.json':
                        files_placement.append(workdir+'/'+d+'/'+f)
        # if we used several chunks, we need to merge placements to produce one
        # unified insertion tree in the end
        if len(files_placement) > 1:
            sys.stderr.write("step 1) merging placement files: ")
            static = None
            placements = []
            for file_placement in files_placement:
                f = open(file_placement, 'r')
                plcmnts = json.loads(f.read())
                f.close()
                placements.extend(plcmnts['placements'])
                if static is None:
                    del plcmnts['placements']
                    static = plcmnts
            with open('%s/all_placements.json' % (workdir), 'w') as outfile:
                static['placements'] = placements
                json.dump(static, outfile)
            sys.stderr.write(' done.\n')

            sys.stderr.write("step 2) placing fragments into tree: ...")
            # guppy ran for: and consumed 45 GB of memory for 2M, chunked 10k
            # sepp benchmark:
            # real	37m39.772s
            # user	31m3.906s
            # sys	3m49.602s
            cluster_run([
                ('$HOME/miniconda3/envs/%s/lib/python3.5/site-packages/'
                 'q2_fragment_insertion/assets/sepp-package/sepp/tools/'
                 'bundled/Linux/guppy-64 tog -o '
                 '%s/all_tree.nwk '
                 '%s/all_placements.json') % (environment,  workdir, workdir)],
                environment=environment,
                jobname='guppy_rename',
                result="%s/all_tree.nwk" % workdir,
                ppn=1, pmem='100GB', walltime='1:00:00',
                dry=dry,
                wait=True, use_grid=use_grid)
            sys.stderr.write(' done.\n')
        else:
            sys.stderr.write("step 1+2) extracting newick tree: ")
            cluster_run([('qiime tools export '
                          '%s/res_1/tree.qza '
                          '--output-dir %s/res_1/') %
                         (workdir, workdir),
                         ('mv %s/res_1/tree.nwk %s/all_tree.nwk') %
                         (workdir, workdir)],
                        environment=environment,
                        jobname='extract',
                        result="%s/all_tree.nwk" % workdir,
                        ppn=1, dry=dry,
                        wait=True, use_grid=False)
            sys.stderr.write(' done.\n')

        sys.stderr.write("step 3) merge taxonomy: ")
        taxonomies = []
        for file_taxonomy in next(os.walk(workdir))[2]:
            if file_taxonomy.startswith('taxonomy_') and \
               file_taxonomy.endswith('.tsv'):
                taxonomies.append(pd.read_csv(workdir + '/' + file_taxonomy,
                                  sep="\t", index_col=0))
        taxonomy = pd.concat(taxonomies)
        sys.stderr.write(' done.\n')

        f = open("%s/all_tree.nwk" % workdir, 'r')
        tree = f.readlines()[0].strip()
        f.close()

        return {'taxonomy': taxonomy,
                'tree': tree}

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()

    seqs = inp
    if type(inp) != pd.Series:
        seqs = pd.Series(inp, index=inp).sort_index()
    if reference_alignment is not None:
        reference_alignment = os.path.abspath(reference_alignment)
    if reference_phylogeny is not None:
        reference_phylogeny = os.path.abspath(reference_phylogeny)
    if reference_taxonomy is not None:
        reference_taxonomy = os.path.abspath(reference_taxonomy)
    args = {'seqs': seqs,
            'reference_alignment': reference_alignment,
            'reference_phylogeny': reference_phylogeny,
            'reference_taxonomy': reference_taxonomy,
            'chunksize': chunksize}
    return _executor('sepp',
                     args,
                     pre_execute,
                     commands,
                     post_execute,
                     ppn=ppn, pmem=pmem, walltime=walltime,
                     array=len(range(0, seqs.shape[0], chunksize)),
                     environment=environment,
                     **executor_args)


def sepp_stepbystep(counts, reference=None,
                    stopdecomposition=None,
                    ppn=20, pmem='8GB', walltime='12:00:00',
                    **executor_args):
    """Step by Step version of SEPP to track memory consumption more closely.
       Tip insertion of deblur sequences into GreenGenes backbone tree.

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    reference : str
        Default: None.
        Valid values are ['pynast']. Use a different alignment file for SEPP.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose
    chunksize: int
        Default: 30000
        SEPP jobs seem to fail if too many sequences are submitted per job.
        Therefore, we can split your sequences in chunks of chunksize.

    Returns
    -------
    ???"""
    def pre_execute(workdir, args):
        file_fragments = workdir + '/sequences.mfa'
        f = open(file_fragments, 'w')
        for header, sequence in seqs.iteritems():
            f.write('>%s\n%s\n' % (header, sequence))
        f.close()
        os.makedirs(workdir + '/sepp-tempssd/', exist_ok=True)

    def commands(workdir, ppn, args):
        commands = []
        name = 'seppstepbysteprun'
        dir_base = ('/home/sjanssen/miniconda3/envs/seppGG_py3/'
                    'src/sepp-package/')
        dir_tmp = workdir + '/sepp-tempssd/'

        commands.append('cd %s' % workdir)

        commands.append(
            ('python %s -P %i -A %s -t %s -a %s -r %s -f %s -cp '
             '%s/chpoint-%s -o %s -d %s -p %s '
             '1>>%s/sepp-%s-out.log 2>%s/sepp-%s-err.log') % (
                ('%ssepp/run_sepp.py' % dir_base),  # python script of SEPP
                5000,  # problem size for tree
                1000,  # problem size for alignment
                # reference tree file
                ('%sref/reference-gg-raxml-bl-rooted-relabelled.tre' %
                    dir_base),
                # reference alignment file
                ('%sref/gg_13_5_ssu_align_99_pfiltered.fasta' % dir_base),
                # reference info file
                ('%sref/RAxML_info-reference-gg-raxml-bl.info' % dir_base),
                workdir + '/sequences.mfa',  # sequence input file
                dir_tmp,  # tmpdir
                name,
                name,
                workdir,
                dir_tmp,
                workdir,
                name,
                workdir,
                name))

        commands.append(('%s/sepp/tools/bundled/Linux/guppy-64 tog %s/%s_plac'
                         'ement.json') % (dir_base, workdir, name))
        commands.append(('python %s/%s_rename-json.py < %s/%s_placement.tog.t'
                         're > %s/%s_placement.tog.relabelled.tre') %
                        (workdir, name, workdir, name, workdir, name))
        commands.append(('%s/sepp/tools/bundled/Linux/guppy-64 tog --xml %s/%'
                         's_placement.json') % (dir_base, workdir, name))
        commands.append(('python %s/%s_rename-json.py < %s/%s_placement.tog.x'
                         'ml > %s/%s_placement.tog.relabelled.xml') %
                        (workdir, name, workdir, name, workdir, name))

        return commands

    def post_execute(workdir, args):
        file_merged_tree = workdir +\
            '/seppstepbysteprun_placement.tog.relabelled.tre'
        sys.stderr.write("step 1/2) reading skbio tree: ...")
        tree = TreeNode.read(file_merged_tree)
        sys.stderr.write(' done.\n')

        sys.stderr.write("step 2/2) use the phylogeny to det"
                         "ermine tips lineage: ")
        lineages = []
        features = []
        divisor = int(tree.count(tips=True) / min(10, tree.count(tips=True)))
        for i, tip in enumerate(tree.tips()):
            if i % divisor == 0:
                sys.stderr.write('.')
            if tip.name.isdigit():
                continue

            lineage = []
            for ancestor in tip.ancestors():
                try:
                    float(ancestor.name)
                except TypeError:
                    pass
                except ValueError:
                    lineage.append(ancestor.name)

            lineages.append("; ".join(reversed(lineage)))
            features.append(tip.name)
        sys.stderr.write(' done.\n')

        # storing tree as newick string is necessary since large trees would
        # result in too many recursions for the python heap :-/
        newick = StringIO()
        tree.write(newick)
        return {'taxonomy': pd.DataFrame(data=lineages,
                                         index=features,
                                         columns=['taxonomy']),
                'tree': newick.getvalue(),
                'reference': args['reference']}

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()

    seqs = inp
    if type(inp) != pd.Series:
        seqs = pd.Series(inp, index=inp).sort_index()
    return _executor('seppstep',
                     {'seqs': seqs,
                      'reference': reference},
                     pre_execute,
                     commands,
                     post_execute,
                     ppn=ppn, pmem=pmem, walltime=walltime,
                     **executor_args)


def sepp_git(counts,
             ppn=20, pmem='8GB', walltime='12:00:00',
             **executor_args):
    """Latest git version of SEPP.
       Tip insertion of deblur sequences into GreenGenes backbone tree.

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    ???"""
    def pre_execute(workdir, args):
        file_fragments = workdir + '/sequences.mfa'
        f = open(file_fragments, 'w')
        for header, sequence in seqs.iteritems():
            f.write('>%s\n%s\n' % (header, sequence))
        f.close()
        os.makedirs(workdir + '/sepp-tempssd/', exist_ok=True)

    def commands(workdir, ppn, args):
        commands = []
        commands.append('cd %s' % workdir)
        commands.append('%srun-sepp.sh "%s" res -x %i' % (
            ('/home/sjanssen/Benchmark_insertiontree/'
             'Software/sepp/sepp-package/'),
            workdir+'/sequences.mfa',
            ppn))
        return commands

    def post_execute(workdir, args):
        file_merged_tree = workdir +\
            '/res_placement.tog.relabelled.tre'
        sys.stderr.write("step 1/2) reading skbio tree: ...")
        tree = TreeNode.read(file_merged_tree)
        sys.stderr.write(' done.\n')

        sys.stderr.write("step 2/2) use the phylogeny to det"
                         "ermine tips lineage: ")
        lineages = []
        features = []
        divisor = int(tree.count(tips=True) / min(10, tree.count(tips=True)))
        for i, tip in enumerate(tree.tips()):
            if i % divisor == 0:
                sys.stderr.write('.')
            if tip.name.isdigit():
                continue

            lineage = []
            for ancestor in tip.ancestors():
                try:
                    float(ancestor.name)
                except TypeError:
                    pass
                except ValueError:
                    lineage.append(ancestor.name)

            lineages.append("; ".join(reversed(lineage)))
            features.append(tip.name)
        sys.stderr.write(' done.\n')

        # storing tree as newick string is necessary since large trees would
        # result in too many recursions for the python heap :-/
        newick = StringIO()
        tree.write(newick)
        return {'taxonomy': pd.DataFrame(data=lineages,
                                         index=features,
                                         columns=['taxonomy']),
                'tree': newick.getvalue()}

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()

    seqs = inp
    if type(inp) != pd.Series:
        seqs = pd.Series(inp, index=inp).sort_index()
    return _executor('seppgit',
                     {'seqs': seqs},
                     pre_execute,
                     commands,
                     post_execute,
                     environment='sepp_git',
                     ppn=ppn, pmem=pmem, walltime=walltime,
                     **executor_args)


def sortmerna(sequences,
              reference='/projects/emp/03-otus/reference/97_otus.fasta',
              sortmerna_db='/projects/emp/03-otus/reference/97_otus.idx',
              ppn=5, pmem='20GB', walltime='2:00:00', **executor_args):
    """Assigns closed ref GreenGenes OTUids to sequences.

    Parameters
    ----------
    sequences : Pd.Series
        Set of sequences with header as index and nucleotide sequences as
        values.
    reference : filename
        Default: /projects/emp/03-otus/reference/97_otus.fasta
        Multiple fasta collection that serves as reference for sortmerna
        homology searches.
    sortmerna_db : filename
        Default: /projects/emp/03-otus/reference/97_otus.idx
        Can point to a precompiled reference DB. Make sure it matches your
        reference collection! Saves ~25min compute.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    """
    def pre_execute(workdir, args):
        # store all unique sequences to a fasta file
        file_fragments = workdir + '/sequences.mfa'
        file_mapping = workdir + '/headermap.tsv'
        f = open(file_fragments, 'w')
        m = open(file_mapping, 'w')
        for i, (header, sequence) in enumerate(args['seqs'].iteritems()):
            f.write('>%s\n%s\n' % ('seq_%i' % i, sequence))
            m.write('seq_%i\t%s\n' % (i, header))
        f.close()
        m.close()

    def commands(workdir, ppn, args):
        commands = []
        precompileddb = ''
        if args['sortmerna_db'] is not None:
            precompileddb = ' --sortmerna_db %s ' % args['sortmerna_db']
        commands.append(('pick_otus.py '
                         '-m sortmerna '
                         '-i %s '
                         '-r %s '
                         '%s'
                         '-o %s '
                         '--sortmerna_e_value 1 '
                         '-s 0.97 '
                         '--threads %i ') % (
            workdir + '/sequences.mfa',
            args['reference'],
            precompileddb,
            workdir + '/sortmerna/',
            ppn))
        return commands

    def post_execute(workdir, args):
        assignments = []

        # parse header mapping file
        hmap = pd.read_csv(workdir + '/headermap.tsv', sep='\t', header=None,
                           index_col=0)
        # parse sucessful sequence to OTU assignments
        f = open(workdir+'/sortmerna/sequences_otus.txt', 'r')
        for line in f.readlines():
            parts = line.rstrip().split('\t')
            for header in parts[1:]:
                assignments.append({'otuid': parts[0],
                                    'header': hmap.loc[header].iloc[0]})
        f.close()

        # parse failed sequences
        f = open(workdir+'/sortmerna/sequences_failures.txt', 'r')
        for line in f.readlines():
            assignments.append({'header': hmap.loc[line.rstrip()].iloc[0]})
        f.close()

        return pd.DataFrame(assignments).set_index('header')

    if not os.path.exists(reference):
        raise ValueError('Reference multiple fasta file "%s" does not exist!' %
                         reference)

    if sortmerna_db is not None:
        if not os.path.exists(sortmerna_db+'.stasts'):
            sys.stderr.write('Could not find SortMeRNA precompiled DB. '
                             'I continue by creating a new DB.')
    # core dump with 8GB with 10 nodes, 4h
    # trying 20GB with 10 nodes ..., 4h (long wait for scheduler)
    # trying 20GB with 5 nodes, 2h ...
    if sortmerna_db is not None:
        sortmerna_db = os.path.abspath(sortmerna_db)
    if reference is not None:
        reference = os.path.abspath(reference)
    return _executor('sortmerna',
                     {'seqs': sequences.drop_duplicates().sort_index(),
                      'reference': reference,
                      'sortmerna_db': sortmerna_db},
                     pre_execute,
                     commands,
                     post_execute,
                     ppn=ppn,
                     pmem=pmem,
                     walltime=walltime,
                     **executor_args)


def denovo_tree(counts, ppn=1, **executor_args):
    """Builds a de novo tree for given sequences using PyNAST + fasttree.

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    A newick string of the created phylogenetic tree."""
    def pre_execute(workdir, args):
        # store all unique sequences to a fasta file
        # as in sortmerna we need a header map, because fasttree will otherwise
        # throw strange errors
        file_fragments = workdir + '/sequences.mfa'
        file_mapping = workdir + '/headermap.tsv'
        f = open(file_fragments, 'w')
        m = open(file_mapping, 'w')
        for i, (header, sequence) in enumerate(args['seqs'].iteritems()):
            f.write('>%s\n%s\n' % ('seq%i' % i, sequence))
            m.write('seq%i\t%s\n' % (i, header))
        f.close()
        m.close()

    def commands(workdir, ppn, args):
        commands = []

        commands.append('parallel_align_seqs_pynast.py -O %i -i %s -o %s' % (
            ppn,
            workdir+'/sequences.mfa',
            workdir))
        commands.append('fasttree -nt %s > %s' % (
            workdir+'/sequences_aligned.fasta',
            workdir+'/tree.newick'))

        return commands

    def post_execute(workdir, args):
        # load resulting tree
        f = open(workdir+'/tree.newick', 'r')
        tree = "".join(f.readlines())
        f.close()

        # parse header mapping file and rename sequence identifier
        hmap = pd.read_csv(workdir + '/headermap.tsv', sep='\t', header=None,
                           index_col=0)[1]
        return {'tree': tree,
                'hmap': hmap}

    def post_cache(cache_results):
        hmap = cache_results['results']['hmap']
        tree = TreeNode.read(StringIO(cache_results['results']['tree']))
        for node in tree.tips():
            node.name = hmap.loc[node.name]

        cache_results['results']['tree'] = tree
        del cache_results['results']['hmap']
        return cache_results

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()

    seqs = inp
    if type(inp) != pd.Series:
        seqs = pd.Series(inp, index=inp).sort_index()

    return _executor('pynastfasttree',
                     {'seqs': seqs},
                     pre_execute,
                     commands,
                     post_execute,
                     post_cache=post_cache,
                     ppn=ppn,
                     **executor_args)


def denovo_tree_qiime2(counts, **executor_args):
    """Builds a de novo tree for given sequences using mafft + fasttree
       following the Qiime2 tutorial https://docs.qiime2.org/2017.9/tutorials
       /moving-pictures/#generate-a-tree-for-phylogenetic-diversity-analyses

    Parameters
    ----------
    counts : Pandas.DataFrame | Pandas.Series
        a) OTU counts in form of a Pandas.DataFrame.
        b) If providing a Pandas.Series, we expect the index to be a fasta
           headers and the colum the fasta sequences.
    executor_args:
        dry, use_grid, nocache, wait, walltime, ppn, pmem, timing, verbose

    Returns
    -------
    A newick string of the created phylogenetic tree."""
    def pre_execute(workdir, args):
        # store all unique sequences to a fasta file
        # as in sortmerna we need a header map, because fasttree will otherwise
        # throw strange errors
        file_fragments = workdir + '/sequences.mfa'
        file_mapping = workdir + '/headermap.tsv'
        f = open(file_fragments, 'w')
        m = open(file_mapping, 'w')
        for i, (header, sequence) in enumerate(args['seqs'].iteritems()):
            f.write('>%s\n%s\n' % ('seq%i' % i, sequence.upper()))
            m.write('seq%i\t%s\n' % (i, header))
        f.close()
        m.close()

    def commands(workdir, ppn, args):
        commands = []

        # import fasta sequences into qza
        commands.append(
            ('qiime tools import '
             '--input-path %s '
             '--output-path %s/rep-seqs '
             '--type "FeatureData[Sequence]"') %
            (workdir + '/sequences.mfa', workdir))
        # First, we perform a multiple sequence alignment of the sequences in
        # our FeatureData[Sequence] to create a FeatureData[AlignedSequence]
        # QIIME 2 artifact. Here we do this with the mafft program.
        commands.append(
            ('qiime alignment mafft '
             '--i-sequences %s/rep-seqs.qza '
             '--o-alignment %s/aligned-rep-seqs.qza '
             '--p-n-threads %i') %
            (workdir, workdir, ppn))
        # Next, we mask (or filter) the alignment to remove positions that are
        # highly variable. These positions are generally considered to add
        # noise to a resulting phylogenetic tree.
        commands.append(
            ('qiime alignment mask '
             '--i-alignment %s/aligned-rep-seqs.qza '
             '--o-masked-alignment %s/masked-aligned-rep-seqs.qza') %
            (workdir, workdir))
        # Next, we’ll apply FastTree to generate a phylogenetic tree from the
        # masked alignment.
        commands.append(
            ('qiime phylogeny fasttree '
             '--i-alignment %s/masked-aligned-rep-seqs.qza '
             '--o-tree %s/unrooted-tree.qza '
             '--p-n-threads %i') %
            (workdir, workdir, ppn))
        # The FastTree program creates an unrooted tree, so in the final step
        # in this section we apply midpoint rooting to place the root of the
        # tree at the midpoint of the longest tip-to-tip distance in the
        # unrooted tree.
        commands.append(
            ('qiime phylogeny midpoint-root '
             '--i-tree %s/unrooted-tree.qza '
             '--o-rooted-tree %s/rooted-tree.qza ') %
            (workdir, workdir))

        # export the phylogeny
        commands.append(
            ('qiime tools export '
             '%s/rooted-tree.qza '
             '--output-dir %s') %
            (workdir, workdir))

        return commands

    def post_execute(workdir, args):
        # load resulting tree
        f = open(workdir+'/tree.nwk', 'r')
        tree = "".join(f.readlines())
        f.close()

        # parse header mapping file and rename sequence identifier
        hmap = pd.read_csv(workdir + '/headermap.tsv', sep='\t', header=None,
                           index_col=0)[1]
        return {'tree': tree,
                'hmap': hmap}

    def post_cache(cache_results):
        hmap = cache_results['results']['hmap']
        tree = TreeNode.read(StringIO(cache_results['results']['tree']))
        for node in tree.tips():
            node.name = hmap.loc[node.name]

        cache_results['results']['tree'] = tree
        del cache_results['results']['hmap']
        return cache_results

    inp = sorted(counts.index)
    if type(counts) == pd.Series:
        # typically, the input is an OTU table with index holding sequences.
        # However, if provided a Pandas.Series, we expect index are sequence
        # headers and single column holds sequences.
        inp = counts.sort_index()

    seqs = inp
    if type(inp) != pd.Series:
        seqs = pd.Series(inp, index=inp).sort_index()

    return _executor('qiime2denovo',
                     {'seqs': seqs},
                     pre_execute,
                     commands,
                     post_execute,
                     post_cache=post_cache,
                     environment=QIIME2_ENV,
                     **executor_args)


def _parse_cmpcat_table(lines, fieldname):
    header = lines[0].split()
    columns = lines[1].replace(' < ', '  ').split()
    columns[0] = fieldname
    residuals = lines[2].split()
    residuals[0] = fieldname  # + '_residuals'
    res = []
    for (_type, line) in zip(['field', 'residuals'], [columns, residuals]):
        r = dict()
        r['type'] = _type
        i = 0
        for name in ['field'] + header:
            if _type == 'residuals':
                if name in ['F.Model', 'Pr(>F)', 'F_value', 'F', 'N.Perm']:
                    r[name] = np.nan
                    continue
            r[name] = line[i]
            i += 1
        res.append(r)
    return pd.DataFrame(res).loc[:, ['field', 'type'] + header]


def _parse_adonis(filename, fieldname='unnamed'):
    """Parse the R result of an adonis test.

    Parameters
    ----------
    filename : str
        Filepath to R adonis output.
    fieldname: str
        Name for the field that has been tested.

    Returns
    -------
        Pandas.DataFrame holding adonis results.
        Two rows: first is the tested field, second the residuals."""
    f = open(filename, 'r')
    lines = f.readlines()
    f.close()
    res = _parse_cmpcat_table(lines[9:12], fieldname)
    res['method'] = 'adonis'

    return res


def _parse_permdisp(filename, fieldname='unnamed'):
    f = open(filename, 'r')
    lines = f.readlines()
    f.close()

    # fix header names, i.e. remove white spaces for later splitting
    for i in [3, 12]:
        lines[i] = lines[i]\
            .replace('Sum Sq', 'Sum_Sq')\
            .replace('Mean Sq', 'Mean_Sq')\
            .replace('F value', 'F_value')
    upper = _parse_cmpcat_table(lines[3:6], fieldname)
    upper['method'] = 'permdisp'
    upper['kind'] = 'observed'
    lower = _parse_cmpcat_table(lines[12:15], fieldname)
    lower['method'] = 'permdisp'
    lower['kind'] = 'permuted'
    lower = lower.rename(columns={'F': 'F_value'})

    return pd.concat([upper, lower])


def _parse_permanova(filename, fieldname='unnamed'):
    res = pd.read_csv(filename, sep='\t', header=None).T
    res.columns = res.iloc[0, :]
    res = res.iloc[1:, :]
    del res['method name']
    res['method'] = 'permanova'
    res['field'] = fieldname
    return res


def compare_categories(beta_dm, metadata,
                       methods=['adonis', 'permanova', 'permdisp'],
                       num_permutations=999, **executor_args):
    """Tests for significance of a metadata field regarding beta diversity.

    Parameters
    ----------
    beta_dm : skbio.stats.distance._base.DistanceMatrix
        The beta diversity distance matrix for the samples
    metadata : pandas.DataFrame
        Metadata columns to be checked for variation.
    methods : [str]
        Default: ['adonis', 'permanova', 'permdisp'].
        Choose from ['adonis', 'permanova', 'permdisp'].
        The statistical test that should be applied.
    num_permutations : int
        Number of permutations to use for permanova test.

    Returns
    -------
    """
    def pre_execute(workdir, args):
        dm = args['beta_dm']
        meta = args['metadata']
        # only use samples present in both:
        # the distance metrix and the metadata
        idx = set(dm.ids) & set(meta.index)
        # make sure both objects have the same sorting of samples
        dm = dm.filter(idx, strict=False)
        meta = meta.loc[idx, :]

        dm.write(workdir + '/beta_distances.txt')
        meta.to_csv(workdir + '/meta.tsv',
                    sep="\t", index_label="#SampleID", header=True)
        f = open(workdir + '/fields.txt', 'w')
        f.write("\n".join(meta.columns)+"\n")
        f.close()

    def commands(workdir, ppn, args):
        commands = []

        commands.append('module load R_3.3.0')
        commands.append('cd %s' % workdir)
        for method in args['methods']:
            commands.append(
                ('compare_categories.py --method %s '
                 '-i %s/beta_distances.txt '
                 '-m %s/meta.tsv '
                 '-c `cat fields.txt | head -n ${PBS_ARRAYID} '
                 '| tail -n 1` '
                 '-o %s/res%s_`cat fields.txt | head -n ${PBS_ARRAYID} '
                 '| tail -n 1`/ '
                 '-n %i') %
                (method, workdir, workdir, workdir, method, num_permutations))

        return commands

    def post_execute(workdir, args):
        merged = dict()

        ms = zip(['adonis', 'permdisp', 'permanova'],
                 [_parse_adonis, _parse_permdisp, _parse_permanova])
        for (name, method) in list(ms):
            merged[name] = []
            for field in args['metadata'].columns:
                filename_result = '%s/res%s_%s/%s_results.txt' % (
                    workdir, name, field, name)
                if os.path.exists(filename_result):
                    merged[name].append(method(filename_result, field))
            merged[name] = pd.concat(merged[name])
        return merged

    if type(metadata) == pd.core.series.Series:
        metadata = metadata.to_frame()

    return _executor('cmpcat',
                     {'beta_dm': beta_dm,
                      'metadata':
                      metadata[sorted(metadata.columns)].sort_index(),
                      'num_permutations': num_permutations,
                      'methods': sorted(methods)},
                     pre_execute,
                     commands,
                     post_execute,
                     ppn=1,
                     array=len(range(0, metadata.shape[1])),
                     **executor_args)


def _parse_timing(workdir, jobname):
    """If existant, parses timing information.

    Parameters
    ----------
    workdir : str
        Path to tmp workdir of _executor containing cr_ana_<jobname>.t* file
    jobname : str
        Name of ran job.

    Parameters
    ----------
    None if file could not be found. Otherwise: [str]
    """
    files_timing = [workdir + '/' + d
                    for d in next(os.walk(workdir))[2]
                    if 'cr_ana_%s.t' % jobname in d]
    for file_timing in files_timing:
        with open(file_timing, 'r') as content_file:
            return content_file.readlines()
        # stop after reading first found file, since there should only be one
        break
    return None


def _executor(jobname, cache_arguments, pre_execute, commands, post_execute,
              post_cache=None,
              dry=True, use_grid=True, ppn=10, nocache=False,
              pmem='8GB', environment=QIIME_ENV, walltime='4:00:00',
              wait=True, timing=True, verbose=True, array=1, dirty=False):
    """

    Parameters
    ----------
    jobname : str
    cache_arguments : []
    pre_execute : function
    commands : []
    post_execute : function
    post_cache : function
        A function that is called, after results have been loaded from cache /
        were generated. E.g. drawing rarefaction curves.
    environment : str

    ==template arguments that should be copied to calling analysis function==
    dry : bool
        Default: True.
        If True: only prepare working directory and create necessary input
        files and print the command that would be executed in a non dry run.
        For debugging. Workdir is not deleted.
        "pre_execute" is called, but not "post_execute".
    use_grid : bool
        Default: True.
        If True, use qsub to schedule as a grid job, otherwise run locally.
    nocache : bool
        Default: False.
        Normally, successful results are cached in .anacache directory to be
        retrieved when called a second time. You can deactivate this feature
        (useful for testing) by setting "nocache" to True.
    wait : bool
        Default: True.
        Wait for results.
    walltime : str
        Default: "12:00:00".
        hh:mm:ss formated wall runtime on cluster.
    ppn : int
        Default: 10.
        Number of CPU cores to be used.
    pmem : str
        Default: '8GB'.
        Resource request for cluster jobs. Multiply by ppn!
    timing : bool
        Default: True
        Use '/usr/bin/time' to log run time of commands.
    verbose : bool
        Default: True
        If True, report progress on sys.stderr.
    array : int
        Default: 1 = deactivated.
        Only for Torque submits: make the job an array job.
        You need to take care of correct use of ${PBS_JOBID} !
    dirty : bool
        Defaul: False.
        If True, temporary working directory will not be removed.

    Returns
    -------
    """
    DIR_CACHE = '.anacache'
    FILE_STATUS = 'finished.info'
    results = {'results': None,
               'workdir': None,
               'qid': None,
               'file_cache': None,
               'timing': None,
               'cache_version': 20170817,
               'created_on': None,
               'jobname': jobname}

    # create an ID function if no post_cache function is supplied
    def _id(x):
        return x
    if post_cache is None:
        post_cache = _id

    # phase 1: compute signature for cache file
    # convert skbio.DistanceMatrix object to a sorted version of its data for
    # hashing
    cache_args_original = dict()
    for arg in cache_arguments.keys():
        if type(cache_arguments[arg]) == DistanceMatrix:
            cache_args_original[arg] = cache_arguments[arg]
            dm = cache_arguments[arg]
            cache_arguments[arg] = dm.filter(sorted(dm.ids)).data

    _input = collections.OrderedDict(sorted(cache_arguments.items()))
    results['file_cache'] = "%s/%s.%s" % (DIR_CACHE, hashlib.md5(
        str(_input).encode()).hexdigest(), jobname)

    # convert back cache arguments if necessary
    for arg in cache_args_original.keys():
        cache_arguments[arg] = cache_args_original[arg]

    # phase 2: if cache contains matching file, load from cache and return
    if os.path.exists(results['file_cache']) and (nocache is not True):
        if verbose:
            sys.stderr.write("Using existing results from '%s'. \n" %
                             results['file_cache'])
        f = open(results['file_cache'], 'rb')
        results = pickle.load(f)
        f.close()
        return post_cache(results)

    # phase 3: search in TMP dir if non-collected results are
    # ready or are waited for
    dir_tmp = tempfile.gettempdir()
    if use_grid:
        dir_tmp = os.environ['HOME'] + '/TMP/'

    # collect all tmp workdirs that contain the right cache signature
    pot_workdirs = [x[0]  # report directory name
                    for x in os.walk(dir_tmp)
                    # shares same cache signature:
                    if results['file_cache'].split('/')[-1] in x[2]]
    finished_workdirs = []
    for wd in pot_workdirs:
        all_finished = True
        for i in range(array):
            if not os.path.exists(wd+'/finished.info%i' % (i+1)):
                all_finished = False
                break
        if all_finished:
            finished_workdirs.append(wd)
    if len(pot_workdirs) > 0 and len(finished_workdirs) <= 0:
        if verbose:
            sys.stderr.write(
                ('Found %i temporary working directories, but non of '
                 'them have finished. If no job is currently running,'
                 ' you might want to delete these directories and res'
                 'tart:\n  %s\n') % (len(pot_workdirs),
                                     "\n  ".join(pot_workdirs)))
        return results
    if len(finished_workdirs) > 0:
        # arbitrarily pick first found workdir
        results['workdir'] = finished_workdirs[0]
        if verbose:
            sys.stderr.write('found matching working dir "%s"\n' %
                             results['workdir'])
    else:
        # create a temporary working directory
        prefix = 'ana_%s_' % jobname
        results['workdir'] = tempfile.mkdtemp(prefix=prefix, dir=dir_tmp)
        if verbose:
            sys.stderr.write("Working directory is '%s'. " %
                             results['workdir'])
        # leave an empty file in workdir with cache file name to later
        # parse results from tmp dir
        f = open("%s/%s" % (results['workdir'],
                            results['file_cache'].split('/')[-1]), 'w')
        f.close()

        pre_execute(results['workdir'], cache_arguments)

        lst_commands = commands(results['workdir'], ppn, cache_arguments)
        # device creation of a file _after_ execution of the job in workdir
        lst_commands.append('touch %s/%s${PBS_ARRAYID}' %
                            (results['workdir'], FILE_STATUS))
        results['qid'] = cluster_run(
            lst_commands, 'ana_%s' % jobname, results['workdir']+'mock',
            environment, ppn=ppn, wait=wait, dry=dry,
            pmem=pmem, walltime=walltime,
            file_qid=results['workdir']+'/cluster_job_id.txt',
            timing=timing,
            file_timing=results['workdir']+('/timing${PBS_ARRAYID}.txt'),
            array=array, use_grid=use_grid)
        if dry:
            return results
        if wait is False:
            return results

    results['results'] = post_execute(results['workdir'],
                                      cache_arguments)
    results['created_on'] = datetime.datetime.fromtimestamp(
        time.time()).strftime('%Y-%m-%d %H:%M:%S')

    results['timing'] = []
    for timingfile in next(os.walk(results['workdir']))[2]:
        if timingfile.startswith('timing'):
            with open(results['workdir']+'/'+timingfile, 'r') as content_file:
                results['timing'] += content_file.readlines()

    if results['results'] is not None:
        if not dirty:
            shutil.rmtree(results['workdir'])
            if verbose:
                sys.stderr.write(" Was removed.\n")

    os.makedirs(os.path.dirname(results['file_cache']), exist_ok=True)
    f = open(results['file_cache'], 'wb')
    pickle.dump(results, f)
    f.close()

    return post_cache(results)
