from unittest import TestCase, main
import warnings
import matplotlib.pyplot as plt
import tempfile
import random
import os
import sys
import numpy as np
from io import StringIO
from tempfile import mkstemp
from os import remove
from biom.table import Table
from biom.util import biom_open

from skbio.util import get_data_path
import pandas as pd

from ggmap.snippets import pandas2biom, biom2pandas, plotTaxonomy
from ggmap.imgdiff import compare_images

plt.switch_backend('Agg')
plt.rc('font', family='DejaVu Sans')


class TaxPlotTests(TestCase):
    def setUp(self):
        pass

    def test_plotTaxonomy_amina(self):
        def get_depth(bodysite):
            minreads = 70000
            if bodysite == 'Arm':
                minreads = 65000
            elif bodysite == 'Armpit':
                minreads = 20000
            elif bodysite == 'Face':
                minreads = 42000
            elif bodysite == 'Foot':
                minreads = 26800
            return minreads

        meta = pd.read_csv(get_data_path('amina.meta.tsv'),
                           index_col=0, sep='\t')
        for bodysite in sorted(meta['sample_site'].unique()):
            res = plotTaxonomy(
                get_data_path('amina.sub10k.biom'),
                meta[meta['sample_site'] == bodysite],
                file_taxonomy=get_data_path('amina.taxonomy.cr.tsv'),
                rank='Family',
                group_l0='hsid',
                group_l1='phase',
                fct_aggregate=np.mean,
                minreadnr=get_depth(bodysite),
                grayscale=False,
                no_sample_numbers=True,
                min_abundance_grayscale=0.0005)

            file_plot = mkstemp('__amina_%s_nogray.png' % bodysite)[1]
            res[0].savefig(file_plot)
            file_diff = mkstemp('_diff_amina_%s_nogray.png' % bodysite)[1]
            cmp = compare_images(
                file_plot,
                get_data_path('plot_baseline/amina_%s_nogray.png' % bodysite),
                file_image_diff=file_diff,
                threshold=0)

            print(cmp)
            break

if __name__ == '__main__':
    main()
