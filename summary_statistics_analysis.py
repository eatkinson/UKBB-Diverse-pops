#!/usr/bin/env python3

__author__ = 'konradk'

import argparse
from pprint import pprint

from gnomad.utils import slack
from ukb_common import *
from ukbb_pan_ancestry import *

def remove_phenos_from_analysis(mt: hl.MatrixTable):
    pass


def annotate_mt_with_largest_meta_analysis(mt, h2_filter='none'):
    if h2_filter == 'both':
        raise ValueError('h2_filter must be none or pass for annotate_mt_with_largest_meta_analysis')
    #meta_mt = hl.read_matrix_table(get_meta_analysis_results_path(h2_filter))
    meta_mt = load_meta_analysis_results(h2_filter=h2_filter, exponentiate_p=True)
    meta_analysis_join = meta_mt[mt.row_key, mt.col_key].meta_analysis
    return mt.annotate_entries(meta=hl.or_missing(hl.len(meta_analysis_join) > 0, meta_analysis_join[0]))


def get_sig_pops(mt):
    return (hl.enumerate(mt.summary_stats)
            .filter(lambda x: x[1].Pvalue < 5e-8)
            .map(lambda x: mt.pheno_data[x[0]].pop))


def get_sig_pops_clumped(mt):
    return (hl.enumerate(mt.plink_clump)
            .filter(lambda x: (x[1].P < 5e-8) & (hl.len(mt.clump_pops[x[0]]) == 1))
            .map(lambda x: mt.clump_pops[x[0]][0]))


def partial_log_log(p):
    neglog_p = -hl.log10(p)
    return hl.if_else(neglog_p > 10, 10 * hl.log10(neglog_p), neglog_p)


def compute_sig_pops_by_pheno(overwrite):
    mt = load_final_sumstats_mt(separate_columns_by_pop=False, exponentiate_p=True, filter_pheno_h2_qc=False, filter_phenos=False)
    # clump_mt = hl.read_matrix_table(get_clumping_results_path(high_quality=True, not_pop=False))
    clump_mt = hl.read_matrix_table('gs://ukb-diverse-pops-public/clump_results_high_quality_22115/full.mt')
    mt = all_axis_join(mt, clump_mt)
    mt = mt.annotate_entries(
        sig_pops=get_sig_pops(mt),
        sig_pops_clumped=get_sig_pops_clumped(mt)
    )
    mt = mt.annotate_cols(
        sig_pops_by_pheno_grouped=hl.agg.counter(hl.delimit(hl.sorted(mt.sig_pops))),
        sig_pops_by_pheno_total=hl.agg.explode(lambda x: hl.agg.counter(x), mt.sig_pops),
        sig_pops_by_pheno_clumped=hl.agg.explode(lambda x: hl.agg.counter(x), mt.sig_pops_clumped)
    )
    mt.cols().write(get_analysis_data_path('sig_hits', 'sig_hits_pops_by_pheno', 'full', 'ht'), overwrite=overwrite)
    ht = hl.read_table(get_analysis_data_path('sig_hits', 'sig_hits_pops_by_pheno', 'full', 'ht'))
    # explode over pheno_data and index into sig_pops_by_pheno_clumped
    ht = ht.explode(ht.pheno_data)
    ht = ht.transmute(
        pop=ht.pheno_data.pop,
        total=ht.sig_pops_by_pheno_total.get(ht.pheno_data.pop),
        clumped=ht.sig_pops_by_pheno_clumped.get(ht.pheno_data.pop)
    ).drop('clump_pops', 'sig_pops_by_pheno_grouped')
    ht.export(get_analysis_data_path('sig_hits', 'sig_hits_pops_by_pheno', 'full'))


def compute_number_of_variants_by_pop(overwrite):
    ht = hl.read_table(get_variant_results_qc_path())
    ht = ht.annotate(**{f'freq_{pop}': ht.freq.find(lambda x: x.pop == pop).af for pop in POPS})
    ht.aggregate(hl.struct(
        pop1=hl.agg.count_where(ht.freq_EUR > 0.01),
        pop2=hl.agg.count_where((ht.freq_EUR > 0.01) | (ht.freq_CSA > 0.01)),
        pop3=hl.agg.count_where((ht.freq_EUR > 0.01) | (ht.freq_CSA > 0.01) | (ht.freq_AFR > 0.01)),
        pop4=hl.agg.count_where((ht.freq_EUR > 0.01) | (ht.freq_CSA > 0.01) | (ht.freq_AFR > 0.01) | (ht.freq_EAS > 0.01)),
        pop5=hl.agg.count_where((ht.freq_EUR > 0.01) | (ht.freq_CSA > 0.01) | (ht.freq_AFR > 0.01) | (ht.freq_EAS > 0.01) | (ht.freq_MID > 0.01)),
        pop6=hl.agg.count_where((ht.freq_EUR > 0.01) | (ht.freq_CSA > 0.01) | (ht.freq_AFR > 0.01) | (ht.freq_EAS > 0.01) | (ht.freq_MID > 0.01) | (ht.freq_AMR > 0.01))
    ))


def compute_top_p(overwrite):
    """
    Workhorse function to compute statistics on the top p-value for each variant (row-wise). Computes:

    - top p-value for each population across all traits (as array `top_p`)
    - top meta-analysis p-value across all traits (`top_meta_p`)
      - notably, this brings each population's results with it in a sub-field `pop_data`

    :param bool overwrite: Whether to overwrite results
    """
    mt = load_final_sumstats_mt(separate_columns_by_pop=False, add_only_gene_symbols_as_str=True, exponentiate_p=True)

    mt = annotate_mt_with_largest_meta_analysis(mt, 'pass')
    sumstats_with_pop = hl.zip_with_index(mt.summary_stats).map(
        lambda x: x[1].annotate(pop=mt.pheno_data[x[0]].pop)
    )
    sumstats_with_pop_pheno_meta = sumstats_with_pop.map(
        lambda x: x.annotate(meta=mt.meta, **mt.col_key, **{x: mt[x] for x in PHENO_DESCRIPTION_FIELDS})
    )
    sumstats_with_pheno_meta_by_pop = [sumstats_with_pop_pheno_meta.find(lambda ss: ss.pop == pop) for pop in POPS]
    meta_with_pheno_and_per_pop = mt.meta.annotate(
        pop_data=sumstats_with_pop,
        **mt.col_key, **{x: mt[x] for x in PHENO_DESCRIPTION_FIELDS})

    def agg_top_p(ss_pop, significant_only: bool = False):
        p = -hl.abs(ss_pop.BETA / ss_pop.SE)
        top_ss = hl.agg.take(ss_pop, 1, ordering=p)
        p_filter = hl.is_defined(p) & ~hl.is_nan(p)
        if significant_only:
            p_filter &= (p < 5e-8)
        return hl.agg.filter(p_filter, hl.or_missing(hl.len(top_ss) > 0, top_ss[0]))

    ht = mt.annotate_rows(
        top_p=[agg_top_p(ss_pop) for ss_pop in sumstats_with_pheno_meta_by_pop],
        top_meta_p=agg_top_p(meta_with_pheno_and_per_pop),
        # top_p_6pop_phenos=[hl.agg.filter(hl.len(mt.pheno_data) == 6, agg_top_p(ss_pop)) for ss_pop in sumstats_with_pheno_meta_by_pop],
        # top_meta_p_6pop_phenos=hl.agg.filter(hl.len(mt.pheno_data) == 6, agg_top_p(meta_with_pheno_and_per_pop))
    ).rows().naive_coalesce(1000).drop('vep', 'freq')
    ht.write(get_analysis_data_path('sig_hits', 'top_p_by_variant', 'full', 'ht'), overwrite=overwrite)

def compute_variances():
    mt = hl.read_matrix_table(get_ukb_pheno_mt_path())

    # compute across all individuals
    mt_all = mt.annotate_cols(stats_all_indiv = hl.agg.stats(mt.both_sexes)) 
    ht_all = mt_all.select_cols(var_all = mt_all.stats_all_indiv.stdev ** 2).cols()

    # compute per population
    ht_pop = mt.group_rows_by('pop').aggregate(stats_per_pop=hl.agg.stats(mt.both_sexes)).entries() 
    ht_pop = ht_pop.annotate(var_pop = ht_pop.stats_per_pop.stdev ** 2)
    ht_pop = ht_pop.key_by('trait_type', 'phenocode', 'pheno_sex', 'coding', 'modifier')
    ht_pop = ht_pop.select(ht_pop.pop, ht_pop.var_pop).collect_by_key(name = "var_pop")

    # compute per leave-one-out population groupings
    mt_loo = mt.annotate_rows(membership=[mt.pop != pop for pop in POPS])
    mt_loo = mt_loo.annotate_cols(loo_stats_array=[hl.agg.filter(mt_loo.membership[i], hl.agg.stats(mt_loo.both_sexes)) for i, pop in enumerate(POPS)]) 
    mt_loo = mt_loo.annotate_cols(var_loo = mt_loo.loo_stats_array.stdev ** 2, loo_pop = [hl.agg.filter(mt_loo.membership[i] == False, hl.agg.take(mt_loo.pop, 1)) for i, pop in enumerate(POPS)])
    ht_loo = mt_loo.select_cols(var_loo_pop = hl.zip(mt_loo.loo_pop, mt_loo.var_loo).map(lambda x: hl.struct(loo_pop=x[0], var=x[1]))).cols()

    # join 
    ht_var = ht_all.join(ht_pop).join(ht_loo)

    return ht_var

def compute_neff():
    mt = load_final_sumstats_mt()
    mt2 = hl.read_matrix_table(get_ukb_pheno_mt_path())

    # calculate per-population variances
    ht_pop = mt2.group_rows_by('pop').aggregate(stats_per_pop=hl.agg.stats(mt2.both_sexes)).entries() 
    ht_pop = ht_pop.annotate(var = ht_pop.stats_per_pop.stdev ** 2)
    ht_pop = ht_pop.key_by(*PHENO_KEY_FIELDS)
    ht_pop = ht_pop.select(ht_pop.pop, ht_pop.var).collect_by_key(name = "var_pop") 

    # add variances to sumstats mt
    mt_w_var = mt.annotate_cols(**ht_pop[mt.col_key])

    # compute Neff
    mt_w_var = mt_w_var.explode_cols(mt_w_var.var_pop)
    mt_w_var = mt_w_var.filter_cols(mt_w_var.var_pop.pop == mt_w_var.pheno_data.pop)
    mt_w_var = mt_w_var.annotate_cols(neff = mt_w_var.var_pop.var * hl.agg.mean( 1 / ((mt_w_var.summary_stats.SE**2) * 2 * mt_w_var.summary_stats.AF_Allele2 * (1 - mt_w_var.summary_stats.AF_Allele2))))

    return mt_w_var.cols()

def main(args):
    hl.init(log=hl.utils.timestamp_path(os.path.join('/tmp', 'hail'), suffix='.log'), default_reference='GRCh37',
            gcs_requester_pays_configuration='ukbb-diversepops-neale')

    if args.write_gene_intervals:
        create_genome_intervals_file().write(get_gene_intervals_path('GRCh37'), args.overwrite)

    # Figure 1
    if args.generate_pheno_summary:
        mt = load_final_sumstats_mt(filter_phenos=False, separate_columns_by_pop=False, annotate_with_nearest_gene=False, filter_pheno_h2_qc=False)
        mt.cols().explode('pheno_data').flatten().export(get_analysis_data_path('phenos', 'pheno_summary', 'full'))

    if args.compute_sig_pops_by_pheno:
        compute_sig_pops_by_pheno(args.overwrite)

    # Extended Data Figure 3
    if args.compute_number_of_variants_by_pop:
        compute_number_of_variants_by_pop(args.overwrite)

    if args.compute_top_p:
        compute_top_p(args.overwrite)

    if args.export_top_p:
        # Export top meta-analysis p-value for each variant
        # Also grabs the most significant population that contributed to that meta-analysis
        ht_full = hl.read_table(get_analysis_data_path('sig_hits', 'top_p_by_variant', 'full', 'ht'))

        ht = locus_alleles_to_chr_pos_ref_alt(ht_full.annotate(global_position=ht_full.locus.global_position()),
                                              True)
        ht = ht.select('chrom', 'pos', 'ref', 'alt', 'nearest_genes',
                       **ht.top_meta_p.drop(*PHENO_DESCRIPTION_FIELDS))
        ht = ht.filter((hl.len(ht.pop_data) > 0) & (ht.Pvalue < 0.01))
        ht = ht.transmute(top_pop=hl.sorted(ht.pop_data, key=lambda x: x.Pvalue)[0])
        ht = ht.filter(~ht.top_pop.low_confidence)
        ht.flatten().export(get_analysis_data_path('sig_hits', 'top_meta_with_top_pop_by_variant', 'full'))

    # Figure 3
    if args.meta_eur_comparison:
        mt_meta = hl.read_matrix_table(get_meta_analysis_results_path(filter_pheno_h2_qc=True))
        mt_meta = mt_meta.annotate_rows(
            max_maf_pop=mt_meta.freq.filter(
                lambda x: (0.5 - hl.abs(0.5 - x.af)) == hl.max((0.5 - hl.abs(0.5 - mt_meta.freq.af)))
            ).pop[0]
        )
        mt_meta = mt_meta.annotate_entries(
            meta_analysis=mt_meta.meta_analysis[0].annotate(
                Pvalue=mt_meta.meta_analysis[0].Pvalue / -np.log(10),
                Pvalue_het=mt_meta.meta_analysis[0].Pvalue_het / -np.log(10),
            ),
            EUR=mt_eur[mt_meta.row_key, mt_meta.col_key].summary_stats,
        )
        mt_meta = mt_meta.filter_entries(
            (mt_meta.meta_analysis.Pvalue > nlog10p_threshold) | (mt_meta.EUR.Pvalue > nlog10p_threshold)
        )
        mt_meta = mt_meta.checkpoint("gs://ukb-diverse-pops/misc/comp/meta_eur_comparison.mt", overwrite=True)

    if args.export_meta_eur_comparison:
        mt_meta = hl.read_matrix_table("gs://ukb-diverse-pops/misc/comp/meta_eur_comparison.mt")
        clump_mt = hl.read_matrix_table(get_clumping_results_path(not_pop=False, high_quality=True))
        clump_mt = clump_mt.select_entries(
            in_clump=hl.or_missing(hl.is_defined(clump_mt.plink_clump[-1]),
                                   hl.delimit(clump_mt.clump_pops[-1]))
        )
        mt_meta = all_axis_join(mt_meta, clump_mt)

        pop_lds = [hl.read_table(get_ld_score_ht_path(pop)) for pop in POPS]
        models = [ht.aggregate(hl.agg.linreg(ht.ld_score, [1, ht.AF]), _localize=False) for ht in pop_lds]
        pop_lds = [ht.annotate(ld_score_resid=ht.ld_score - (model.beta[0] + model.beta[1] * ht.AF)) for ht, model
                   in
                   zip(pop_lds, models)]
        mt_meta = annotate_nearest_gene(mt_meta, add_only_gene_symbols_as_str=True)
        ht_meta = mt_meta.key_cols_by().entries()
        ht_meta = ht_meta.select(
            *PHENO_KEY_FIELDS, 'description', 'high_quality', 'max_maf_pop', 'info', 'nearest_genes', 'in_clump',
            Pvalue_meta=ht_meta.meta_analysis.Pvalue,
            Pvalue_EUR=ht_meta.EUR.Pvalue,
            beta_meta=ht_meta.meta_analysis.BETA,
            beta_EUR=ht_meta.EUR.BETA,
            SE_meta=ht_meta.meta_analysis.SE,
            SE_EUR=ht_meta.EUR.SE,
            low_confidence_EUR=ht_meta.EUR.low_confidence,
            Pvalue_het=ht_meta.meta_analysis.Pvalue_het,
            AF_Allele2_meta=ht_meta.meta_analysis.AF_Allele2,
            AF_Allele2_EUR=ht_meta.EUR.AF_Allele2,
            N_pops=ht_meta.meta_analysis.N_pops,
            pops=hl.delimit(ht_meta.meta_analysis_data[0].pop),
            **{f'ldscore_{pop}': pop_lds[i][ht_meta.key].ld_score for i, pop in enumerate(POPS)},
            **{f'ldscore_resid_{pop}': pop_lds[i][ht_meta.key].ld_score_resid for i, pop in enumerate(POPS)},
            **{f'freq_{pop}': ht_meta.freq.find(lambda x: x.pop == pop).af for pop in POPS}
        )
        ht_meta = locus_alleles_to_chr_pos_ref_alt(ht_meta, unkey_drop_and_add_as_prefix=True)
        ht_meta.describe()
        ht_meta.export("gs://ukb-diverse-pops/misc/comp/meta_eur_comparison.tsv.bgz")


    # TODO: add clumping and re-compute top p meta vs top p EUR
    if args.export_top_p_eur:
        ht_full = hl.read_table(get_analysis_data_path('sig_hits', 'top_p_by_variant', 'full', 'ht'))

        ht = locus_alleles_to_chr_pos_ref_alt(ht_full.annotate(global_position=ht_full.locus.global_position()), True)
        ht = ht.transmute(**ht.top_meta_p).drop('top_p')
        ht = ht.transmute(eur_data=ht.pop_data.find(lambda x: x.pop == 'EUR'))

        # Downsample in EUR p vs meta p partial-log-log space
        ht2 = downsample_table_by_x_y(ht, partial_log_log(ht.eur_data.Pvalue), partial_log_log(ht.Pvalue),
                                      {'nearest_genes': ht.nearest_genes,
                                       **{x: hl.str(ht[x]) for x in ('chrom', 'pos', 'ref', 'alt') + PHENO_KEY_FIELDS + PHENO_DESCRIPTION_FIELDS}},
                                      x_field_name='eur_pvalue_ll', y_field_name='meta_pvalue_ll', n_divisions=500)
        ht2.export(get_analysis_data_path('sig_hits', 'eur_p_vs_meta_p', 'EUR'))

        ht2 = downsample_table_by_x_y(ht, ht.global_position, partial_log_log(ht.Pvalue),
                                      {'nearest_genes': ht.nearest_genes,
                                       **{x: hl.str(ht[x]) for x in ('chrom', 'pos', 'ref', 'alt') + PHENO_KEY_FIELDS + PHENO_DESCRIPTION_FIELDS}},
                                      x_field_name='global_position', y_field_name='meta_pvalue_ll', n_divisions=500)
        ht2.export(get_analysis_data_path('sig_hits', 'downsampled_manhattan_p', 'EUR'))

    if args.export_top_p:
        return
        ht_full = hl.read_table(get_analysis_data_path('sig_hits', 'top_p_by_variant', 'full', 'ht'))

        ht = locus_alleles_to_chr_pos_ref_alt(ht_full.annotate(global_position=ht_full.locus.global_position()), True)
        # Get variants that are 5e-8 in meta, but not in EUR
        ht = ht.filter((ht.eur_data.Pvalue > 5e-8) & (ht.Pvalue < 5e-8)).flatten()
        ht.export(get_analysis_data_path('sig_hits', 'newly_significant_variants', 'full'))

        ht_full = locus_alleles_to_chr_pos_ref_alt(ht_full.filter(ht_full.locus.contig == '2'), True)
        # Top meta p value by variant
        ht = ht_full.filter(ht_full.top_meta_p.Pvalue < 0.01)
        ht.transmute(**ht.top_meta_p).drop('top_p', 'pop_data').export(
            get_analysis_data_path('sig_hits', 'top_meta_p_by_variant', 'full'))

        # Top meta p value by variant with data from each population
        ht.transmute(**ht.top_meta_p).drop('top_p').explode('pop_data').flatten().export(
            get_analysis_data_path('sig_hits', 'top_meta_p_by_variant_with_pop', 'full'))

        # Top p value for each population (with meta)
        ht = ht_full.explode('top_p')
        ht = ht.filter(ht.top_p.Pvalue < 0.01)
        ht.transmute(**ht.top_p.annotate(meta_for_top_pheno_for_pop=ht.top_p.meta).drop('meta', 'description_more'),
                     **{f'meta_{k}': v for k, v in ht.top_meta_p.items() if k != 'description_more'}).flatten().export(
            get_analysis_data_path('sig_hits', 'top_p_by_variant_by_pop', 'full'))


    if args.meta_analysis_hits:
        mt = load_final_sumstats_mt(separate_columns_by_pop=False, add_only_gene_symbols_as_str=True)#, load_contig='22')
        mt = annotate_mt_with_largest_meta_analysis(mt)
        mt = mt.select_entries(sig_meta=hl.or_else(mt.meta.Pvalue < 5e-8, False),
                               sig_eur=hl.or_else(get_sig_pops(mt).contains('EUR'), False))
        pprint(mt.aggregate_entries(hl.struct(
            significant_associations=hl.agg.counter(mt.entry),
            significant_loci=hl.len(hl.agg.filter(mt.sig_meta, hl.agg.collect_as_set(mt.row_key))),
            significant_loci_EUR=hl.len(hl.agg.filter(mt.sig_eur, hl.agg.collect_as_set(mt.row_key))),
            significant_loci_not_EUR=hl.len(hl.agg.filter(mt.sig_meta & ~mt.sig_eur, hl.agg.collect_as_set(mt.row_key))),
            significant_traits=hl.len(hl.agg.filter(mt.sig_meta, hl.agg.collect_as_set(mt.col_key))),
            significant_traits_EUR=hl.len(hl.agg.filter(mt.sig_eur, hl.agg.collect_as_set(mt.col_key))),
            significant_traits_not_EUR = hl.len(hl.agg.filter(mt.sig_meta & ~mt.sig_eur, hl.agg.collect_as_set(mt.col_key))),
        )))
        # {'significant_associations': {Struct(sig_meta=True, sig_eur=False): 901199,
        #                               Struct(sig_meta=False, sig_eur=False): 108905474188,
        #                               Struct(sig_meta=True, sig_eur=True): 12934323,
        #                               Struct(sig_meta=False, sig_eur=True): 1056739},
        #  'significant_loci': 1790087,
        #  'significant_loci_EUR': 1826305,
        #  'significant_loci_not_EUR': 527517,
        #  'significant_traits': 3850,
        #  'significant_traits_EUR': 3752,
        #  'significant_traits_not_EUR': 1725}


    if args.beta_correlations:
        mt = load_final_sumstats_mt(separate_columns_by_pop=False, annotate_with_nearest_gene=False)
        pop_indices = hl.range(hl.len(mt.pheno_data.pop))

        def get_polarized_linreg(mt, index1, index2):
            sumstats1 = mt.summary_stats[index1].BETA
            sumstats2 = mt.summary_stats[index2].BETA
            flip = sumstats1 < 0
            sumstats1 = hl.if_else(flip, -sumstats1, sumstats1)
            sumstats2 = hl.if_else(flip, -sumstats2, sumstats2)
            return hl.agg.linreg(sumstats2, [1.0, sumstats1])

        p_thresholds = [5e-8, 1e-6, 1e-5, 1e-4, 1e-3, 0.01, 0.05, 1]

        ht = mt.transmute_cols(pairwise_corr=[hl.agg.array_agg(
            lambda index1: hl.agg.array_agg(
                lambda index2: hl.agg.filter(
                    (mt.summary_stats[index1].Pvalue < p_threshold) & (mt.summary_stats[index2].Pvalue < p_threshold),
                    get_polarized_linreg(mt, index1, index2)),
                pop_indices),
            pop_indices) for p_threshold in p_thresholds]).cols()

        def zip_and_explode(ht, zip_field1, zip_field2, output_field1, output_field2):
            ht = ht.annotate(new_field=hl.zip(zip_field1, zip_field2)).explode('new_field')
            return ht.transmute(**{output_field1: ht.new_field[0], output_field2: ht.new_field[1]})

        ht = zip_and_explode(ht, p_thresholds, ht.pairwise_corr, 'p_value_threshold', 'pairwise_corr')
        ht = zip_and_explode(ht, ht.pheno_data, ht.pairwise_corr, 'pop1', 'pairwise_corr')
        ht = zip_and_explode(ht, ht.pheno_data, ht.pairwise_corr, 'pop2', 'pairwise_corr')
        ht.write(get_analysis_data_path('effect_size', 'pairwise_beta_corr', 'full', 'ht'), args.overwrite)
        ht = hl.read_table(get_analysis_data_path('effect_size', 'pairwise_beta_corr', 'full', 'ht'))
        # TODO: figure out directionality of beta (pop1 ~ pop2, or vv)
        ht = ht.annotate(intercept=ht.pairwise_corr.beta[0], beta=ht.pairwise_corr.beta[1],
                         intercept_p=ht.pairwise_corr.p_value[0], beta_p=ht.pairwise_corr.p_value[1]).drop('pheno_data')
        ht.flatten().export(get_analysis_data_path('effect_size', 'pairwise_beta_corr', 'full'))


    # if args.hits_by_pheno:
    #     mt = load_final_sumstats_mt(separate_columns_by_pop=False, add_only_gene_symbols_as_str=True)
    #     mt = mt.annotate_entries(
    #         sig_pops=get_sig_pops(mt)
    #     )
    #     mt = mt.group_rows_by(
    #         contig=mt.locus.contig,
    #         genes=mt.nearest_genes,
    #     ).partition_hint(100).aggregate(
    #         sig_pops=hl.agg.explode(lambda x: hl.agg.collect_as_set(x), mt.sig_pops)
    #     )
    #     ht = mt.filter_entries(hl.len(mt.sig_pops) > 0).drop('pheno_data', 'pheno_indices').entries()
    #     ht = ht.checkpoint(get_analysis_data_path('sig_hits', 'sig_hits_by_pheno', 'full', 'ht'), overwrite=True)
    #     ht.annotate(sig_pops=hl.delimit(hl.sorted(hl.array(ht.sig_pops)))).export(
    #         get_analysis_data_path('sig_hits', 'sig_hits_by_pheno', 'full')
    #     )


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('--overwrite', help='Overwrite everything', action='store_true')
    parser.add_argument('--write_gene_intervals', help='Overwrite everything', action='store_true')
    parser.add_argument('--compute_number_of_variants_by_pop', help='Overwrite everything', action='store_true')
    parser.add_argument('--meta_eur_comparison', help='Overwrite everything', action='store_true')
    parser.add_argument('--export_meta_eur_comparison', help='Overwrite everything', action='store_true')
    parser.add_argument('--generate_pheno_summary', help='Overwrite everything', action='store_true')
    parser.add_argument('--compute_top_p', help='Overwrite everything', action='store_true')
    parser.add_argument('--export_top_p_eur', help='Overwrite everything', action='store_true')
    parser.add_argument('--export_top_p', help='Overwrite everything', action='store_true')
    parser.add_argument('--compute_sig_pops_by_pheno', help='Overwrite everything', action='store_true')
    parser.add_argument('--meta_analysis_hits', help='Overwrite everything', action='store_true')
    parser.add_argument('--beta_correlations', help='Overwrite everything', action='store_true')
    parser.add_argument('--hits_by_pheno', help='Overwrite everything', action='store_true')
    parser.add_argument('--slack_channel', help='Send message to Slack channel/user', default='@konradjk')
    args = parser.parse_args()

    if args.slack_channel:
        from slack_token_pkg.slack_creds import slack_token
        with slack.slack_notifications(slack_token, args.slack_channel):
            main(args)
