import os
import numpy as np
from pysam import Samfile, Fastafile
from math import ceil, floor
from Bio import motifs
import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt

import pyx
from scipy import stats
from argparse import SUPPRESS

# Internal
from rgt.Util import ErrorHandler, AuxiliaryFunctions, GenomeData
from rgt.GenomicRegionSet import GenomicRegionSet
from rgt.HINT.biasTable import BiasTable

"""
Perform differential footprints analysis based on the prediction.

Authors: Eduardo G. Gusmao, Zhijian Li
"""

dic = {"A": 0, "C": 1, "G": 2, "T": 3}


def diff_analysis_args(parser):
    # Input Options
    parser.add_argument("--organism", type=str, metavar="STRING", default="hg19",
                        help="Organism considered on the analysis. Must have been setup in the RGTDATA folder. "
                             "Common choices are hg19, hg38. mm9, and mm10. DEFAULT: hg19")
    parser.add_argument("--mpbs-file1", type=str, metavar="FILE", default=None,
                        help="motif predicted binding sites file for condition 1, must be .bed file. DEFAULT: None")
    parser.add_argument("--mpbs-file2", type=str, metavar="FILE", default=None,
                        help="motif predicted binding sites file for condition 2, must be .bed file. DEFAULT: None")
    parser.add_argument("--reads-file1", type=str, metavar="FILE", default=None,
                        help="The BAM file containing the DNase-seq or ATAC-seq reads for condition 1. DEFAULT: None")
    parser.add_argument("--reads-file2", type=str, metavar="FILE", default=None,
                        help="The BAM file containing the DNase-seq or ATAC-seq reads for condition 2. DEFAULT: None")
    parser.add_argument("--bias-table1", type=str, metavar="FILE1_F,FILE1_R", default=None,
                        help="Bias table files for condition 1. DEFAULT: None")
    parser.add_argument("--bias-table2", type=str, metavar="FILE2_F,FILE2_R", default=None,
                        help="Bias table files for condition 2. DEFAULT: None")

    parser.add_argument("--window-size", type=int, metavar="INT", default=200,
                        help="The window size for differential analysis. DEFAULT: 200")
    parser.add_argument("--factor1", type=float, metavar="FLOAT", default=None,
                        help="The normalization factor for condition 1. DEFAULT: None")
    parser.add_argument("--factor2", type=float, metavar="FLOAT", default=None,
                        help="The normalization factor for condition 1. DEFAULT: None")

    parser.add_argument("--forward-shift", type=int, metavar="INT", default=5, help=SUPPRESS)
    parser.add_argument("--reverse-shift", type=int, metavar="INT", default=-4, help=SUPPRESS)
    parser.add_argument("--condition1", type=str, metavar="STRING", default="condition1",
                        help="The name of condition1. DEFAULT: condition1")
    parser.add_argument("--condition2", type=str, metavar="STRING", default="condition1",
                        help="The name of condition2. DEFAULT: condition2")
    parser.add_argument("--fdr", type=float, metavar="FLOAT", default=0.001,
                        help="The false discovery rate. DEFAULT: 0.001")

    # Output Options
    parser.add_argument("--output-location", type=str, metavar="PATH", default=os.getcwd(),
                        help="Path where the output bias table files will be written. DEFAULT: current directory")
    parser.add_argument("--output-prefix", type=str, metavar="STRING", default="differential",
                        help="The prefix for results files. DEFAULT: differential")
    parser.add_argument("--standardize", action="store_true", default=False,
                        help="If set, the signal will be rescaled to (0, 1) for plotting.")
    parser.add_argument("--output-profiles", default=False, action='store_true',
                        help="If set, the footprint profiles will be writen into a text, in which each row is a "
                             "specific instance of the given motif. DEFAULT: False")


def diff_analysis_run(args):
    # Initializing Error Handler
    err = ErrorHandler()

    output_location = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2))
    try:
        if not os.path.isdir(output_location):
            os.makedirs(output_location)
    except Exception:
        err.throw_error("MM_OUT_FOLDER_CREATION")

    mpbs1 = GenomicRegionSet("Motif Predicted Binding Sites of Condition1")
    mpbs1.read(args.mpbs_file1)

    mpbs2 = GenomicRegionSet("Motif Predicted Binding Sites of Condition2")
    mpbs2.read(args.mpbs_file2)

    mpbs = mpbs1.combine(mpbs2, output=True)
    mpbs.sort()

    mpbs_name_list = list(set(mpbs.get_names()))

    genome_data = GenomeData(args.organism)
    fasta = Fastafile(genome_data.get_genome())

    bam1 = Samfile(args.reads_file1, "rb")
    bam2 = Samfile(args.reads_file2, "rb")

    signal_dict_by_tf_1 = dict()
    signal_dict_by_tf_2 = dict()
    motif_len_dict = dict()
    pwm_dict_by_tf = dict()

    if args.bias_table1 is None or args.bias_table2 is None:
        # differential analysis using raw reads number
        for mpbs_name in mpbs_name_list:
            signal_dict_by_tf_1[mpbs_name] = list()
            signal_dict_by_tf_2[mpbs_name] = list()
            pwm_dict_by_tf[mpbs_name] = dict([("A", [0.0] * args.window_size), ("C", [0.0] * args.window_size),
                                              ("G", [0.0] * args.window_size), ("T", [0.0] * args.window_size),
                                              ("N", [0.0] * args.window_size)])
            motif_len_dict[mpbs_name] = 0

            mpbs_regions = mpbs.by_names([mpbs_name])
            for region in mpbs_regions:
                if motif_len_dict[mpbs_name] == 0:
                    motif_len_dict[mpbs_name] = region.final - region.initial

                mid = (region.final + region.initial) / 2
                p1 = max(mid - args.window_size / 2, 0)
                p2 = mid + args.window_size / 2

                # Fetch raw signal
                tc1 = np.zeros(args.window_size)
                for read in bam1.fetch(region.chrom, p1, p2):
                    if not read.is_reverse:
                        cut_site = read.pos + args.forward_shift
                        if p1 <= cut_site < p2:
                            tc1[cut_site - p1] += 1.0
                    else:
                        cut_site = read.aend + args.reverse_shift - 1
                        if p1 <= cut_site < p2:
                            tc1[cut_site - p1] += 1.0
                signal_dict_by_tf_1[mpbs_name].append(tc1.tolist())

                tc2 = np.zeros(args.window_size)
                for read in bam2.fetch(region.chrom, p1, p2):
                    if not read.is_reverse:
                        cut_site = read.pos + args.forward_shift
                        if p1 <= cut_site < p2:
                            tc2[cut_site - p1] += 1.0
                    else:
                        cut_site = read.aend + args.reverse_shift - 1
                        if p1 <= cut_site < p2:
                            tc2[cut_site - p1] += 1.0
                signal_dict_by_tf_2[mpbs_name].append(tc2.tolist())
                update_pwm(pwm_dict_by_tf[mpbs_name], fasta, region, p1, p2)
    else:
        # using bias corrected signal
        bias_table1 = None
        bias_table2 = None
        if args.bias_table1:
            table_list = args.bias_table1.split(",")
            bias_table1 = BiasTable().load_table(table_file_name_F=table_list[0], table_file_name_R=table_list[1])
        if args.bias_table2:
            table_list = args.bias_table2.split(",")
            bias_table2 = BiasTable().load_table(table_file_name_F=table_list[0], table_file_name_R=table_list[1])

        for mpbs_name in mpbs_name_list:
            signal_dict_by_tf_1[mpbs_name] = list()
            signal_dict_by_tf_2[mpbs_name] = list()
            pwm_dict_by_tf[mpbs_name] = dict([("A", [0.0] * args.window_size), ("C", [0.0] * args.window_size),
                                              ("G", [0.0] * args.window_size), ("T", [0.0] * args.window_size),
                                              ("N", [0.0] * args.window_size)])
            motif_len_dict[mpbs_name] = 0

            mpbs_regions = mpbs.by_names([mpbs_name])
            for region in mpbs_regions:
                if motif_len_dict[mpbs_name] == 0:
                    motif_len_dict[mpbs_name] = region.final - region.initial

                mid = (region.final + region.initial) / 2
                p1 = max(mid - args.window_size / 2, 0)
                p2 = mid + args.window_size / 2

                # Fetch bias corrected signal
                signal_1 = get_bc_signal(chrom=region.chrom, start=p1, end=p2, bam=bam1,
                                         bias_table=bias_table1, genome_file_name=genome_data.get_genome(),
                                         forward_shift=args.forward_shift, reverse_shift=args.reverse_shift)
                signal_dict_by_tf_1[mpbs_name].append(signal_1)

                signal_2 = get_bc_signal(chrom=region.chrom, start=p1, end=p2, bam=bam2,
                                         bias_table=bias_table2, genome_file_name=genome_data.get_genome(),
                                         forward_shift=args.forward_shift, reverse_shift=args.reverse_shift)
                signal_dict_by_tf_2[mpbs_name].append(signal_2)

                update_pwm(pwm_dict_by_tf[mpbs_name], fasta, region, p1, p2)

    if args.factor1 is None or args.factor2 is None:
        args.factor1, args.factor2 = compute_factors(signal_dict_by_tf_1, signal_dict_by_tf_2)
        output_factor(args, args.factor1, args.factor2)

    if args.output_profiles:
        output_location = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2))
        output_profiles(mpbs_name_list, signal_dict_by_tf_1, output_location, args.condition1)
        output_profiles(mpbs_name_list, signal_dict_by_tf_2, output_location, args.condition2)

    ps_tc_results_by_tf = dict()

    for mpbs_name in mpbs_name_list:
        num_fp = len(signal_dict_by_tf_1[mpbs_name])

        # print the line plot for each factor
        fig, ax = plt.subplots()
        line_plot(args, mpbs_name, num_fp, signal_dict_by_tf_1[mpbs_name], signal_dict_by_tf_2[mpbs_name],
                  pwm_dict_by_tf[mpbs_name], fig, ax)
        plt.close(fig)

        ps_tc_results_by_tf[mpbs_name] = get_ps_tc_results(args, signal_dict_by_tf_1[mpbs_name],
                                                           signal_dict_by_tf_2[mpbs_name], motif_len_dict[mpbs_name])

    output_results(args, ps_tc_results_by_tf)


#    stat_results_by_tf = get_stat_results(ps_tc_results_by_tf)
#    scatter_plot(args, stat_results_by_tf)
#    output_stat_results(args, stat_results_by_tf)


def get_bc_signal(chrom, start, end, bam, bias_table, genome_file_name, forward_shift, reverse_shift):
    # Parameters
    window = 50
    defaultKmerValue = 1.0

    # Initialization
    fastaFile = Fastafile(genome_file_name)
    fBiasDict = bias_table[0]
    rBiasDict = bias_table[1]
    k_nb = len(fBiasDict.keys()[0])
    p1 = start
    p2 = end
    p1_w = p1 - (window / 2)
    p2_w = p2 + (window / 2)
    p1_wk = p1_w - int(floor(k_nb / 2.))
    p2_wk = p2_w + int(ceil(k_nb / 2.))
    if (p1 <= 0 or p1_w <= 0 or p2_wk <= 0):
        # Return raw counts
        bc_signal = [0.0] * (p2 - p1)
        for read in self.bam.fetch(chrName, p1, p2):
            if not read.is_reverse:
                cut_site = read.pos + forward_shift
                if p1 <= cut_site < p2:
                    bc_signal[cut_site - p1] += 1.0
            else:
                cut_site = read.aend + reverse_shift - 1
                if p1 <= cut_site < p2:
                    bc_signal[cut_site - p1] += 1.0

        return bc_signal

    # Raw counts
    nf = [0.0] * (p2_w - p1_w)
    nr = [0.0] * (p2_w - p1_w)
    for read in bam.fetch(chrom, p1_w, p2_w):
        if not read.is_reverse:
            cut_site = read.pos + forward_shift
            if p1_w <= cut_site < p2_w:
                nf[cut_site - p1_w] += 1.0
        else:
            cut_site = read.aend + reverse_shift - 1
            if p1_w <= cut_site < p2_w:
                nr[cut_site - p1_w] += 1.0

    # Smoothed counts
    Nf = []
    Nr = []
    f_sum = sum(nf[:window])
    r_sum = sum(nr[:window])
    f_last = nf[0]
    r_last = nr[0]
    for i in range((window / 2), len(nf) - (window / 2)):
        Nf.append(f_sum)
        Nr.append(r_sum)
        f_sum -= f_last
        f_sum += nf[i + (window / 2)]
        f_last = nf[i - (window / 2) + 1]
        r_sum -= r_last
        r_sum += nr[i + (window / 2)]
        r_last = nr[i - (window / 2) + 1]

    # Fetching sequence
    currStr = str(fastaFile.fetch(chrom, p1_wk, p2_wk - 1)).upper()
    currRevComp = AuxiliaryFunctions.revcomp(str(fastaFile.fetch(chrom, p1_wk + 1, p2_wk)).upper())

    # Iterating on sequence to create signal
    af = []
    ar = []
    for i in range(int(ceil(k_nb / 2.)), len(currStr) - int(floor(k_nb / 2)) + 1):
        fseq = currStr[i - int(floor(k_nb / 2.)):i + int(ceil(k_nb / 2.))]
        rseq = currRevComp[len(currStr) - int(ceil(k_nb / 2.)) - i:len(currStr) + int(floor(k_nb / 2.)) - i]
        try:
            af.append(fBiasDict[fseq])
        except Exception:
            af.append(defaultKmerValue)
        try:
            ar.append(rBiasDict[rseq])
        except Exception:
            ar.append(defaultKmerValue)

    # Calculating bias and writing to wig file
    f_sum = sum(af[:window])
    r_sum = sum(ar[:window])
    f_last = af[0]
    r_last = ar[0]
    bc_signal = []
    for i in range((window / 2), len(af) - (window / 2)):
        nhatf = Nf[i - (window / 2)] * (af[i] / f_sum)
        nhatr = Nr[i - (window / 2)] * (ar[i] / r_sum)
        bc_signal.append(nhatf + nhatr)
        f_sum -= f_last
        f_sum += af[i + (window / 2)]
        f_last = af[i - (window / 2) + 1]
        r_sum -= r_last
        r_sum += ar[i + (window / 2)]
        r_last = ar[i - (window / 2) + 1]

    # Termination
    fastaFile.close()
    return bc_signal


def get_ps_tc_results(args, signal_list_1, signal_list_2, motif_len):
    signal_1 = np.zeros(args.window_size)
    signal_2 = np.zeros(args.window_size)

    num_fp = len(signal_list_1)

    for signal in signal_list_1:
        signal_1 = np.add(signal_1, np.array(signal))
    for signal in signal_list_2:
        signal_2 = np.add(signal_2, np.array(signal))

    signal_1 = (signal_1 / args.factor1) / num_fp
    signal_2 = (signal_2 / args.factor2) / num_fp

    signal_half_len = len(signal_1) / 2

    nc = sum(signal_1[signal_half_len - motif_len / 2:signal_half_len + motif_len / 2])
    nr = sum(signal_1[signal_half_len + motif_len / 2:signal_half_len + motif_len / 2 + motif_len])
    nl = sum(signal_1[signal_half_len - motif_len / 2 - motif_len:signal_half_len - motif_len / 2])

    protect_score1 = (nr - nc) / motif_len + (nl - nc) / motif_len
    tc1 = sum(signal_1) / len(signal_1)

    nc = sum(signal_2[signal_half_len - motif_len / 2:signal_half_len + motif_len / 2])
    nr = sum(signal_2[signal_half_len + motif_len / 2:signal_half_len + motif_len / 2 + motif_len])
    nl = sum(signal_2[signal_half_len - motif_len / 2 - motif_len:signal_half_len - motif_len / 2])

    protect_score2 = (nr - nc) / motif_len + (nl - nc) / motif_len
    tc2 = sum(signal_2) / len(signal_2)

    protect_diff = protect_score1 - protect_score2
    tc_diff = tc1 - tc2
    return [protect_score1, protect_score2, protect_diff, tc1, tc2, tc_diff]


def update_pwm(pwm, fasta, region, p1, p2):
    # Update pwm
    aux_plus = 1
    dna_seq = str(fasta.fetch(region.chrom, p1, p2)).upper()
    if (region.final - region.initial) % 2 == 0:
        aux_plus = 0
    dna_seq_rev = AuxiliaryFunctions.revcomp(str(fasta.fetch(region.chrom,
                                                             p1 + aux_plus, p2 + aux_plus)).upper())
    if region.orientation == "+":
        for i in range(0, len(dna_seq)):
            pwm[dna_seq[i]][i] += 1
    elif region.orientation == "-":
        for i in range(0, len(dna_seq_rev)):
            pwm[dna_seq_rev[i]][i] += 1


def compute_factors(signal_dict_by_tf_1, signal_dict_by_tf_2):
    keys = signal_dict_by_tf_1.keys()

    signal_1 = np.zeros(len(keys))
    signal_2 = np.zeros(len(keys))

    for key in keys:
        for i in range(len(signal_dict_by_tf_1[key])):
            idx = keys.index(key)
            signal_1[idx] += sum(signal_dict_by_tf_1[key][i])
            signal_2[idx] += sum(signal_dict_by_tf_2[key][i])

    # Take log
    log_tc1 = np.log(signal_1)
    log_tc2 = np.log(signal_2)

    # Average
    average_log_tc = np.add(log_tc1, log_tc2) / 2

    # Filter
    filter_log_tc1 = log_tc1[~np.isnan(log_tc1)]
    filter_log_tc2 = log_tc2[~np.isnan(log_tc2)]
    filter_log_tc = average_log_tc[~np.isnan(average_log_tc)]

    # Subtract
    sub_tc1 = np.subtract(filter_log_tc1, filter_log_tc)
    sub_tc2 = np.subtract(filter_log_tc2, filter_log_tc)

    median_tc1 = np.median(sub_tc1)
    median_tc2 = np.median(sub_tc2)

    factor1 = np.exp(median_tc1)
    factor2 = np.exp(median_tc2)

    return factor1, factor2


def line_plot(args, mpbs_name, num_fp, signal_tf_1, signal_tf_2, pwm_dict, fig, ax):
    # compute the average signal
    mean_signal_1 = np.zeros(args.window_size)
    mean_signal_2 = np.zeros(args.window_size)
    for i in range(num_fp):
        mean_signal_1 = np.add(mean_signal_1, signal_tf_1[i])
        mean_signal_2 = np.add(mean_signal_2, signal_tf_2[i])

    mean_signal_1 = (mean_signal_1 / num_fp) / args.factor1
    mean_signal_2 = (mean_signal_2 / num_fp) / args.factor2

    if args.standardize:
        mean_signal_1 = standard(mean_signal_1)
        mean_signal_2 = standard(mean_signal_2)

    # Output PWM and create logo
    pwm_fname = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2),
                             "{}.pwm".format(mpbs_name))
    pwm_file = open(pwm_fname, "w")
    for e in ["A", "C", "G", "T"]:
        pwm_file.write(" ".join([str(int(f)) for f in pwm_dict[e]]) + "\n")
    pwm_file.close()

    logo_fname = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2),
                              "{}.logo.eps".format(mpbs_name))
    pwm = motifs.read(open(pwm_fname), "pfm")
    pwm.weblogo(logo_fname, format="eps", stack_width="large", stacks_per_line=str(args.window_size),
                color_scheme="color_classic", unit_name="", show_errorbars=False, logo_title="",
                show_xaxis=False, xaxis_label="", show_yaxis=False, yaxis_label="",
                show_fineprint=False, show_ends=False)

    start = -(args.window_size / 2)
    end = (args.window_size / 2) - 1
    x = np.linspace(start, end, num=args.window_size)

    ax.plot(x, mean_signal_1, color='red', label=args.condition1)
    ax.plot(x, mean_signal_2, color='blue', label=args.condition2)
    ax.text(0.15, 0.9, 'n = {}'.format(num_fp), verticalalignment='bottom', horizontalalignment='right',
            transform=ax.transAxes, fontweight='bold')

    ax.xaxis.set_ticks_position('bottom')
    ax.yaxis.set_ticks_position('left')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_position(('outward', 15))
    ax.tick_params(direction='out')
    ax.set_xticks([start, 0, end])
    ax.set_xticklabels([str(start), 0, str(end)])
    min_signal = min(min(mean_signal_1), min(mean_signal_2))
    max_signal = max(max(mean_signal_1), max(mean_signal_2))
    ax.set_yticks([min_signal, max_signal])
    ax.set_yticklabels([str(round(min_signal, 2)), str(round(max_signal, 2))], rotation=90)

    ax.set_title(mpbs_name, fontweight='bold')
    ax.set_xlim(start, end)
    ax.set_ylim([min_signal, max_signal])
    ax.legend(loc="upper right", frameon=False)
    ax.spines['bottom'].set_position(('outward', 70))

    figure_name = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2),
                               "{}.line.eps".format(mpbs_name))
    fig.tight_layout()
    fig.savefig(figure_name, format="eps", dpi=300)

    # Creating canvas and printing eps / pdf with merged results
    output_fname = os.path.join(args.output_location, "{}_{}".format(args.condition1, args.condition2),
                                "{}.eps".format(mpbs_name))
    c = pyx.canvas.canvas()
    c.insert(pyx.epsfile.epsfile(0, 0, figure_name, scale=1.0))
    c.insert(pyx.epsfile.epsfile(0.45, 0.8, logo_fname, width=16.5, height=3))
    c.writeEPSfile(output_fname)
    os.system("epstopdf " + output_fname)

    os.remove(figure_name)
    os.remove(logo_fname)
    os.remove(output_fname)
    os.remove(pwm_fname)


def scatter_plot(args, stat_results_by_tf):
    tc_diff = list()
    ps_diff = list()
    mpbs_names = list()
    for mpbs_name in stat_results_by_tf.keys():
        mpbs_names.append(mpbs_name)
        tc_diff.append(stat_results_by_tf[mpbs_name][-2])
        ps_diff.append(stat_results_by_tf[mpbs_name][2])

    fig, ax = plt.subplots(figsize=(12, 12))
    ax.scatter(tc_diff, ps_diff, alpha=0.0)
    for i, mpbs_name in enumerate(mpbs_names):
        if stat_results_by_tf[mpbs_name][-1] < args.fdr:
            ax.annotate(mpbs_name, (tc_diff[i], ps_diff[i]), alpha=0.6)
    ax.margins(0.05)

    tc_diff_mean = np.mean(tc_diff)
    ps_diff_mean = np.mean(ps_diff)
    ax.axvline(x=tc_diff_mean, linewidth=2, linestyle='dashed')
    ax.axhline(y=ps_diff_mean, linewidth=2, linestyle='dashed')

    ax.set_xlabel("TC DIFF of {} - {}".format(args.condition1, args.condition2), fontweight='bold')
    ax.set_ylabel("Protection Score of {} - {}".format(args.condition1, args.condition2), fontweight='bold',
                  rotation=90)

    figure_name = os.path.join(args.output_location, "{}_{}_statistics.pdf".format(args.condition1, args.condition2))
    fig.savefig(figure_name, format="pdf", dpi=300)


def output_results(args, ps_tc_results_by_tf):
    mpbs_name_list = ps_tc_results_by_tf.keys()
    header = ["Motif", "Num",
              "Protection_Score_{}".format(args.condition1), "Protection_Score_{}".format(args.condition2),
              "Protection_Diff_{}_{}".format(args.condition1, args.condition2),
              "TC_{}".format(args.condition1), "TC_{}".format(args.condition2),
              "TC_Diff_{}_{}".format(args.condition1, args.condition2)]
    output_fname = os.path.join(args.output_location, "{}_{}_results.txt".format(args.condition1, args.condition2))
    with open(output_fname, "w") as f:
        f.write("\t".join(header) + "\n")
        for mpbs_name in mpbs_name_list:
            f.write(mpbs_name + "\t" + "\t".join(map(str, ps_tc_results_by_tf[mpbs_name])) + "\n")


def output_stat_results(args, stat_results_by_tf):
    output_fname = os.path.join(args.output_location, "{}_{}_statistics.txt".format(args.condition1, args.condition2))
    header = ["Motif",
              "Protection_Score_{}".format(args.condition1), "Protection_Score_{}".format(args.condition2),
              "Protection_Diff_{}_{}".format(args.condition1, args.condition2),
              "TC_{}".format(args.condition1), "TC_{}".format(args.condition2),
              "TC_Diff_{}_{}".format(args.condition1, args.condition2), "P_values", "Adjust_p_values"]

    with open(output_fname, "w") as f:
        f.write("\t".join(header) + "\n")
        for mpbs_name in stat_results_by_tf.keys():
            f.write(mpbs_name + "\t" + "\t".join(map(str, stat_results_by_tf[mpbs_name])) + "\n")


def output_factor(args, factor1, factor2):
    output_file = os.path.join(args.output_location, "{}_{}_factor.txt".format(args.condition1, args.condition2))
    f = open(output_file, "w")
    f.write("Factor1: " + str(factor1) + "\n")
    f.write("Factor2: " + str(factor2) + "\n")
    f.close()


def output_mu(args, median_diff_prot, median_diff_tc):
    output_file = os.path.join(args.output_location, "{}_{}_mu.txt".format(args.condition1, args.condition2))
    f = open(output_file, "w")
    f.write("median_diff_prot: " + str(median_diff_prot) + "\n")
    f.write("median_diff_tc: " + str(median_diff_tc) + "\n")
    f.close()


def get_stat_results(ps_tc_results_by_tf):
    stat_results_by_tf = dict()
    pop_ps_diff = list()
    pop_tc_diff = list()
    for mpbs_name in ps_tc_results_by_tf.keys():
        stat_results_by_tf[mpbs_name] = list()
        mean_results = np.zeros(6)
        num = 0
        for i in range(len(ps_tc_results_by_tf[mpbs_name])):
            mean_results = np.add(mean_results, np.array(ps_tc_results_by_tf[mpbs_name][i]))
            pop_ps_diff.append(ps_tc_results_by_tf[mpbs_name][i][2])
            pop_tc_diff.append(ps_tc_results_by_tf[mpbs_name][i][-1])
            num += 1
        mean_results = mean_results / num
        stat_results_by_tf[mpbs_name] = mean_results.tolist()

    # Two-sample Hotelling's T-Square test population and specific tf
    population = np.array([pop_ps_diff, pop_tc_diff]).T
    cov_pop = np.cov(population.T)
    n1 = population.shape[0]
    mean1 = population.mean(axis=0)

    for mpbs_name in ps_tc_results_by_tf.keys():
        ps_diff_by_tf = list()
        tc_diff_by_tf = list()
        for i in range(len(ps_tc_results_by_tf[mpbs_name])):
            ps_diff_by_tf.append(ps_tc_results_by_tf[mpbs_name][i][2])
            tc_diff_by_tf.append(ps_tc_results_by_tf[mpbs_name][i][-1])

        tf = np.array([ps_diff_by_tf, tc_diff_by_tf]).T
        cov_tf = np.cov(tf.T)
        n2 = tf.shape[0]
        mean2 = tf.mean(axis=0)

        k = tf.shape[1]
        t2 = np.dot(np.dot((mean1 - mean2).T, np.linalg.inv(cov_pop / n1 + cov_tf / n2)), (mean1 - mean2))
        p_value = stats.chi2.sf(t2, k)
        stat_results_by_tf[mpbs_name].append(p_value)

    p_values = list()
    mpbs_name_list = stat_results_by_tf.keys()
    for mpbs_name in mpbs_name_list:
        p_values.append(stat_results_by_tf[mpbs_name][-1])

    adjusted_p_values = adjust_p_values(p_values)
    for idx, mpbs_name in enumerate(mpbs_name_list):
        stat_results_by_tf[mpbs_name].append(adjusted_p_values[idx])

    return stat_results_by_tf


def standard(vector):
    max_ = max(vector)
    min_ = min(vector)
    if max_ > min_:
        return [(e - min_) / (max_ - min_) for e in vector]
    else:
        return vector


def adjust_p_values(p_values):
    p = np.asfarray(p_values)
    by_descend = p.argsort()[::-1]
    by_orig = by_descend.argsort()
    steps = float(len(p)) / np.arange(len(p), 0, -1)
    q = np.minimum(1, np.minimum.accumulate(steps * p[by_descend]))
    return q[by_orig]


def output_profiles(mpbs_name_list, signal_dict_by_tf, output_location, condition):
    for mpbs_name in mpbs_name_list:
        num_fp = len(signal_dict_by_tf[mpbs_name])
        output_fname = os.path.join(output_location, "{}_{}.txt".format(mpbs_name, condition))
        with open(output_fname, "w") as f:
            for i in range(num_fp):
                f.write("\t".join(map(str, signal_dict_by_tf[mpbs_name][i])) + "\n")
