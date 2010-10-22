#!/usr/bin/env python

__author__ = "Justin Kuzynski"
__copyright__ = "Copyright 2010, The QIIME Project"
__credits__ = ["Justin Kuczynski", "Rob Knight"]
__license__ = "GPL"
__version__ = "1.1.0-dev"
__maintainer__ = "Justin Kuczynski"
__email__ = "justinak@gmail.com"
__status__ = "Development"

"""Contains code for performing beta diversity analyses of different samples.

command line usage help: python beta_diversity.py -h

This module has the responsibility for relating samples to one another. This
is performed using the following inputs:
    - a tab-delimited table of taxon x sample counts (taxa can be OTUs). 
    - future: will be able to supply
      data in (taxon, sample, count) format, sparse representation is
      important for large datasets where full table is impractical to build.
      Note that this requires adapting the beta diversity metrics to work
      with sparse matrices, not yet done.

The output is a sample x sample matrix of distances, incl. row/col headers.
    Note that parser expects first field to be blank, i.e. first char of file
    is expected to be a tab.
"""
from StringIO import StringIO
from qiime.util import FunctionWithParams, TreeMissingError, OtuMissingError
from qiime.format import format_distance_matrix
import cogent.maths.distance_transform #avoid hard-coding metrics
import qiime.beta_metrics
import qiime.beta_diversity
from sys import exit, stderr
import os.path
from cogent.app.util import get_tmp_filename


from qiime.parse import parse_otu_table, parse_newick, PhyloNode
from qiime.format import format_matrix

def get_nonphylogenetic_metric(name):
    """Gets metric by name from cogent.maths.distance_transform.
    
    Metrics should be f(matrix) -> distances.
    """
    # looks for name, inserting possible dist_ to find functions
    # in distance_transform.py named e.g.:
    # binary_dist_chisq / dist_bray_curtis
    try:
        return getattr(cogent.maths.distance_transform, 'dist_' + name.lower())
    except AttributeError:
        try:
            return getattr(cogent.maths.distance_transform, 
              name.replace('binary', 'binary_dist').lower())
        except AttributeError:
            return getattr(cogent.maths.distance_transform, 
              name.lower())

def get_phylogenetic_metric(name):
    """Gets metric by name from cogent.maths.unifrac.fast_unifrac
    
    Metrics should be f(matrix) -> distances.
    """
    # looks for name, inserting possible dist_ to find functions
    # in qiime.beta_metrics
    try:
        return getattr(qiime.beta_metrics, 'dist_' + name.lower())
    except AttributeError:
        try:
            return getattr(qiime.beta_metrics, 
              name.replace('binary', 'binary_dist').lower())
        except AttributeError:
            return getattr(qiime.beta_metrics, 
              name.lower())

def get_phylogenetic_row_metric(name):
    """Gets metric by name from qiime.beta_metrics
    
    Metrics should be f(matrix) -> distance_array.
    """
    # looks for name, inserting one_sample to find functions
    # in qiime.beta_metrics
    return getattr(qiime.beta_metrics, 'one_sample_' + name.lower())

def list_known_nonphylogenetic_metrics():
    """Lists known metrics by name from cogent.maths.distance_transform.

    Assumes that functions starting with dist_ or binary_dist are metrics.
    """
    result = []
    for name in dir(cogent.maths.distance_transform):
        if name.startswith('dist_'):
            result.append(name[5:])
        elif name.startswith('binary_dist_'):
            result.append('binary_' + name[12:])
    result.sort()
    return result

def list_known_phylogenetic_metrics():
    """Lists known phylogenetic metrics from cogent.maths.unifrac."""
    result = []
    for name in dir(qiime.beta_metrics):
        if name.startswith('dist_'):
            result.append(name[5:])
    result.sort()
    return result

def list_known_metrics():
    return list_known_nonphylogenetic_metrics() + \
        list_known_phylogenetic_metrics()

class BetaDiversityCalc(FunctionWithParams):
    """A BetaDiversityCalc takes taxon x sample counts, returns distance mat.

    Some BetaDiversityCalcs also require a tree.

    typical usage is: 
    calc = BetaDiversityCalc(my_metric, 'metric name', \
        IsPhylogenetic=False)
    calc(data_path=stuff, result_path=stuff2)
    """
    _tracked_properties = ['IsPhylogenetic', 'Metric']

    def __init__(self, metric, name, is_phylogenetic=False, params=None):
        """Return new BetaDiversityCalc object with specified params.

        Note: metric must conform to the following interface:
            must return 2d sample distance matrix with sample order preserved.
            * phylogenetic methods: take samples by taxa array, taxon_names, 
            cogent PhyloNode tree
            * nonphylo methods: take only samples by taxa array, numpy 2d

        Not sure what params does
        """
        self.Metric = metric    #should be f(table, tree) -> dist matrix
        self.Name = name
        self.IsPhylogenetic = is_phylogenetic
        self.Params = params or {}

    def getResult(self, data_path, tree_path=None):
        """Returns distance matrix from (indcidence matrix and optionally tree).
        
        Parameters:
        
        data_path: path to data file, matrix (samples = cols, taxa = rows)
        in tab-delimited text format 
        
        tree_path: path or object.
        if method is phylogenetic, must supply tree_path.
        if path, path to
        Newick-format tree file where taxon ids match taxon ids in the
        input data file.

        returns 2d dist matrix, list of sample names ordered as in dist mtx
        """
        #if it's a phylogenetic metric, read the tree
        if self.IsPhylogenetic:
            tree = self.getTree(tree_path)
        else:
            tree = None
        
        sample_names, taxon_names, data, lineages = \
            self.getOtuTable(data_path)
            
        # get the 2d dist matrix from beta diversity analysis
        if self.IsPhylogenetic:
            return (self.Metric(data.T, taxon_names, tree, sample_names),
             sample_names)
        else:
            return self.Metric(data.T), sample_names

    def formatResult(self, result):
        """Generate formatted distance matrix. result is (data, sample_names)"""
        data, sample_names = result
        return format_distance_matrix(sample_names, data)

def single_file_beta(input_path, metrics, tree_path, output_dir, rowids=None,
    full_tree=False):
    """ does beta diversity calc on a single otu table

    uses name in metrics to name output beta diversity files
    assumes input tree is already trimmed to contain only otus present in otu
    table, doesn't call getSubTree()
    inputs:
     input_path (str)
     metrics (str, comma delimited if more than 1 metric)
     tree_path (str)
     output_dir (str)
     rowids (comma separated str)
    """
    f = open(input_path,'U')
    samids, otuids, otumtx, lineages = parse_otu_table(f)
    # otu mtx is otus by samples
    f.close()
    tree = None
    if tree_path:
        f = open(tree_path, 'U')
        tree = parse_newick(f, PhyloNode)
        f.close()
        if not full_tree:
            new_tree_path = get_tmp_filename(suffix='.tre')
            tree = tree.getSubTree(otuids, ignore_missing=True)

    metrics_list = metrics.split(',')
    for metric in metrics_list:
        outfilepath = os.path.join(output_dir, metric + '_' +
            os.path.split(input_path)[1])
        try:
            metric_f = get_nonphylogenetic_metric(metric)
            is_phylogenetic = False
        except AttributeError:
            try:
                metric_f = get_phylogenetic_metric(metric)
                is_phylogenetic = True
                if tree == None:
                    stderr.write("metric %s requires a tree, but none found\n"\
                        % (metric,))
                    exit(1)
            except AttributeError:
                stderr.write("Could not find metric %s.\n\nKnown metrics are: %s\n"\
                    % (metric, ', '.join(list_known_metrics())))
                exit(1)
        if rowids == None:
            # standard, full way
            if is_phylogenetic:
                dissims = metric_f(otumtx.T, otuids, tree, samids)
            else:
                dissims = metric_f(otumtx.T)

            f = open(outfilepath,'w')
            f.write(format_distance_matrix(samids, dissims))
            f.close()
        else:
            # only calc d(rowid1, *) for each rowid
            rowids_list = rowids.split(',')
            row_dissims = [] # same order as rowids_list
            for rowid in rowids_list:
                rowidx = samids.index(rowid)
                
                # first test if we can the dissim is a fn of only the pair
                # if not, just calc the whole matrix
                if metric_f.__name__ == 'dist_chisq' or \
                    metric_f.__name__ == 'dist_gower' or \
                    metric_f.__name__ == 'dist_hellinger' or\
                    metric_f.__name__ == 'binary_dist_chisq':
                    row_dissims.append(metric_f(otumtx.T)[rowidx])
                else:
                    try:
                        row_metric = get_phylogenetic_row_metric(metric)
                    except AttributeError:
                        # do element by element
                        dissims = []
                        for i in range(len(samids)):
                            if is_phylogenetic:
                                dissim = metric_f(otumtx.T[[rowidx,i],:],
                                    otuids, tree, [samids[rowidx],samids[i]])[0,1]
                            else:
                                dissim = metric_f(otumtx.T[[rowidx,i],:])[0,1]
                            dissims.append(dissim)
                        row_dissims.append(dissims)
                    else:
                        # do whole row at once
                        dissims = row_metric(otumtx.T,
                                    otuids, tree, samids, rowid)
                        row_dissims.append(dissims)

            rows_outfilepath = os.path.join(output_dir, metric + '_' +\
                '_'.join(rowids_list) + '_' + os.path.split(input_path)[1])
            f = open(rows_outfilepath,'w')
            f.write(format_matrix(row_dissims,rowids_list,samids))
            f.close()
            
def multiple_file_beta(input_path, output_dir, metrics, tree_path, rowids=None,
    full_tree=False):
    """ runs beta diversity for each input file in the input directory 
    
    performs minimal error checking on input args, then calls single_file_beta
    for each file in the input directory

    inputs:
     input_path (str)
     metrics (str, comma delimited if more than 1 metric)
     tree_path (str)
     output_dir (str)

    """
    metrics_list = metrics.split(',')
    file_names = os.listdir(input_path)
    file_names = [fname for fname in file_names if not fname.startswith('.')]
    try:
        os.makedirs(output_dir)
    except OSError:
        pass # hopefully dir exists
    for metric in metrics_list:
        try:
            metric_f = get_nonphylogenetic_metric(metric)
        except AttributeError:
            try:
                metric_f = get_phylogenetic_metric(metric)
                if not tree_path:
                    raise ValueError("a tree file is required for " + metric)
            except AttributeError:
                raise ValueError(
                    "Could not find metric %s.\n\nKnown metrics are: %s\n" \
                    % (metric, ', '.join(list_known_metrics())))
    
    for fname in file_names:
        single_file_beta(os.path.join(input_path, fname),
            metrics, tree_path, output_dir, rowids, full_tree)
