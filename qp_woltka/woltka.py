# -----------------------------------------------------------------------------
# Copyright (c) 2020--, The Qiita Development Team.
#
# Distributed under the terms of the BSD 3-clause License.
#
# The full license is in the file LICENSE, distributed with this software.
# -----------------------------------------------------------------------------
import pandas as pd

from math import ceil
from os import environ
from os.path import join, basename, exists
from glob import glob
from biom.util import biom_open
from biom import load_table
import re

from qiita_client import ArtifactInfo

# resources per job
PPN = 8
MEMORY = '64g'
WALLTIME = '10:00:00'
MERGE_MEMORY = '48g'
MERGE_WALLTIME = '4:00:00'
MAX_RUNNING = 8


# this is a almost exact copy/paste from the original
# qp_woltka/support_files/to-job-array.py
def _to_array(directory, output, max_running, ppn, walltime, environment,
              command_format, memory, name, output_extension, files):
    # sanity checking
    assert len(files) > 0
    assert ppn > 0
    assert re.match(r'\d+:\d\d:\d\d', walltime) is not None
    assert '{infile}' in command_format
    assert '{outfile}' in command_format

    # 1024 -> maximum number of job array IDs
    max_jobs = 1024
    n_files = len(files)

    # if we have too many files, break pack them into the individual
    # jobs. So, if we had 2000 files, we would create 1000 jobs of
    # each which process 2 files. If we had 1500, we would create
    # 750 jobs each processing 2 files.
    if n_files > max_jobs:
        per_job = int(ceil(n_files / max_jobs))
        n_jobs = int(ceil(n_files / per_job))
    else:
        per_job = 1
        n_jobs = n_files

    # the details describe each input file, and its output file
    details_name = join(output, f'{name}.array-details')
    with open(details_name, 'w') as details:
        for f in files:
            bn = basename(f)
            details.write(f'{f}\t{output}/{bn}.{output_extension}\n')

    # all the setup pieces
    lines = ['#!/bin/bash',
             '#PBS -M qiita.help@gmail.com',
             f'#PBS -N {name}',
             f'#PBS -l nodes=1:ppn={ppn}',
             f'#PBS -l walltime={walltime}',
             f'#PBS -l mem={memory}',
             f'#PBS -o {output}/{name}' + '_${PBS_ARRAYID}.log',
             f'#PBS -e {output}/{name}' + '_${PBS_ARRAYID}.err',
             f'#PBS -t 1-{n_jobs}%{max_running}',
             f'cd {output}',
             f'{environment}',
             'date',  # start time
             'hostname',  # executing system
             'offset=${PBS_ARRAYID}']

    # if we have more than one file per job, we need to adjust our offset
    # position accordingly. If we had three files per job, then the first
    # job processes lines 1, 2, and 3 of the details. The second job
    # processes lines 4, 5, 6. Note that the PBS_ARRAYID is 1-based not
    # 0-based.
    if per_job > 1:
        lines.append(f"offset=$(( $offset * {per_job} ))")

    # reversed due to the substraction with the offset, so that we process
    # lines N, N+1, N+2, etc in the details file.
    for i in reversed(range(per_job)):
        lines.append(f'step=$(( $offset - {i} ))')

        # do not let the last job in the array overstep
        lines.append(f'if [[ $step -gt {n_files} ]]; then exit 0; fi')

        # if we're okay, get the next set of arguments
        lines.append(f'args{i}=$(head -n $step {details_name} | tail -n 1)')

        # f-string is broken up so the awk program is not interpreted
        # as a component of the f-string
        lines.append(
            f"infile{i}=$(echo -e $args{i}" + " | awk '{ print $1 }')")
        lines.append(
            f"outfile{i}=$(echo -e $args{i}" + " | awk '{ print $2 }')")

        # wrap the command calls in "fail on error", and then disable it. The
        # reason to enable (-e) and disable (+e) is some of the other shell
        # scripting may *correctly* produce a nonzero exit status, like the
        # calls to the [ program.
        lines.append('set -e')
        cmd_args = {'infile': f"$infile{i}", 'outfile': f"$outfile{i}"}
        lines.append(command_format.format(**cmd_args))
        lines.append('set +e')
    lines.append('date')  # end time

    # write out the script
    qsub_fp = join(output, f'{name}.qsub')
    with open(qsub_fp, 'w') as job:
        job.write('\n'.join(lines))
        job.write('\n')

    return qsub_fp


def _process_database_files(database_fp):
    files = glob(f'{database_fp}*')
    database_taxonomy = [f for f in files if f.endswith('.tax')][0]
    database_gene_coordinates = [f for f in files if f.endswith('.coords')]
    # not all databases have their coordinates fp
    if database_gene_coordinates:
        database_gene_coordinates = database_gene_coordinates[0]
    else:
        database_gene_coordinates = None

    return database_taxonomy, database_gene_coordinates


def woltka_to_array(directory, output, database_bowtie2,
                    preparation_information, url, name):
    """Creates qsub files for submission of per sample bowtie2 and woltka
    """
    environment = environ["ENVIRONMENT"]
    kwargs = {'directory': directory,
              'output': output,
              'max_running': MAX_RUNNING,
              'ppn': PPN,
              'walltime': WALLTIME,
              'memory': MEMORY,
              'name': name,
              'environment': environment,
              'output_extension': 'sam'}

    database_taxonomy, database_gene_coordinates = _process_database_files(
        database_bowtie2)

    prep = pd.read_csv(preparation_information, sep='\t', dtype=str)

    if 'run_prefix' not in prep.columns:
        raise ValueError(
            'Prep information is missing the required run_prefix column')

    if len(prep.run_prefix.unique()) != prep.shape[0]:
        raise ValueError(
            'The run_prefix values are not unique for each sample')

    kwargs['files'] = [join(directory, rp) for rp in prep.run_prefix.values]

    # woltka assumes R1 and R2 are combined even though it doesn't use the
    # paired end data, so let's concatenate based off the prefixes first.
    # note: 'cat' is safe with gzip'd data, see:
    # https://stackoverflow.com/a/8005155
    concat = 'cat {infile}*.fastq.gz > {outfile}.fastq.gz'

    # Bowtie2 command structure based on shogun settings
    # https://github.com/knights-lab/SHOGUN/blob/ff1aabe772469d6a1c2c83cf146140b5341df83c/shogun/wrappers/bowtie2_wrapper.py#L21-L37
    # And as described in:
    # https://github.com/BenLangmead/bowtie2/issues/311
    bowtie2 = f'bowtie2 -p {PPN} -x {database_bowtie2} ' + \
              '-q {outfile}.fastq.gz -S {outfile}.sam --seed 42 ' + \
              '--very-sensitive -k 16 --np 1 --mp "1,1" ' + \
              '--rdg "0,1" --rfg "0,1" --score-min ' + \
              '"L,0,-0.05" --no-head --no-unal'
    xz = f'xz -9 -T{PPN} -c ' + '{outfile}.sam > {outfile}.xz'

    # Not performing demux as this is per sample, so no need
    ranks = ["phylum", "genus", "species", "free", "none"]
    woltka = 'woltka classify -i {outfile}.sam ' + \
             '-o {outfile}.woltka-taxa ' + \
             '--no-demux ' + \
             f'--lineage {database_taxonomy} ' + \
             f'--rank {",".join(ranks)}'

    # compute per-gene results
    if database_gene_coordinates is not None:
        woltka_per_gene = 'woltka classify -i {outfile}.sam ' + \
                          f'-c {database_gene_coordinates} ' + \
                          '-o {outfile}.woltka-per-gene ' + \
                          '--no-demux'
        cmd_fmt = f'{concat}; {bowtie2}; {woltka}; {woltka_per_gene}; {xz}'
    else:
        cmd_fmt = f'{concat}; {bowtie2}; {woltka}; {xz}'

    # first we'll use the concatenation command, then run bowtie2,
    # finally we'll run woltka
    kwargs['command_format'] = cmd_fmt

    # now, let's establish the merge script.
    merges = []
    merge_inv = f'woltka_merge --prep {preparation_information} ' + \
                f'--base {output} '
    for r in ranks:
        merges.append(" ".join([merge_inv,
                                f'--name {r}',
                                f'--glob "*.woltka-taxa/{r}.biom"',
                                '&']))  # run all at once
    if database_gene_coordinates is not None:
        merges.append(" ".join([merge_inv, '--name per-gene',
                                '--glob "*.woltka-per-gene"',
                                '--rename &']))  # run all at once
    else:
        # for "simplicity" we will inject the `--rename` flag to the last
        # merge command (between all the parameters and the last &)
        m = merges[-1].split(' ')
        merges[-1] = " ".join(m[:-1] + ['--rename'] + [m[-1]])

    # The merge for a HiSeq 2000 lane was 40 seconds and ~150MB of memory.
    # But, let's over request just in case (and this is a very small request
    # relative to the rest of the work).
    n_merges = len(merges)
    assert n_merges < 32  # 32 merges would be crazy...

    lines = ['#!/bin/bash',
             '#PBS -M qiita.help@gmail.com',
             f'#PBS -N merge-{name}',
             f'#PBS -l nodes=1:ppn={n_merges}',
             f'#PBS -l walltime={MERGE_WALLTIME}',
             f'#PBS -l mem={MERGE_MEMORY}',
             f'#PBS -o {output}/merge-{name}.log',
             f'#PBS -e {output}/merge-{name}.err',
             f'cd {output}',
             f'{environment}',
             'date',  # start time
             'hostname',  # executing system
             'set -e',
             '\n'.join(merges),
             "wait",
             f'cd {output}; tar -cvf alignment.tar *.sam.xz\n'
             f'finish_woltka {url} {name} {output}\n'
             "date"]  # end time

    # construct the job array
    main_qsub_fp = _to_array(**kwargs)

    # write out the merge script
    merge_qsub_fp = join(output, f'{name}.merge.qsub')
    with open(merge_qsub_fp, 'w') as out:
        out.write('\n'.join(lines))
        out.write('\n')

    return main_qsub_fp, merge_qsub_fp


def woltka(qclient, job_id, parameters, out_dir):
    """Run Woltka with the given parameters

    Parameters
    ----------
    qclient : tgp.qiita_client.QiitaClient
        The Qiita server client
    job_id : str
        The job id
    parameters : dict
        The parameter values to run split libraries
    out_dir : str
        The path to the job's output directory

    Returns
    -------
    bool, list, str
        The results of the job
    """
    database_taxonomy, database_gene_coordinates = _process_database_files(
        parameters['Database'])

    errors = []
    ainfo = []
    fp_biom = f'{out_dir}/free.biom'
    fp_alng = f'{out_dir}/alignment.tar'
    if exists(fp_biom) and exists(fp_alng):
        ainfo = [ArtifactInfo('Alignment Profile', 'BIOM', [
            (fp_biom, 'biom'), (fp_alng, 'log')])]
    else:
        ainfo = []
        errors.append('Missing files from the "Alignment Profile"; please '
                      'contact qiita.help@gmail.com for more information')

    for rank in ['phylum', 'genus', 'species']:
        fp = f'{out_dir}/{rank}.biom'

        if exists(fp):
            # making sure that the tables have taxonomy
            bt = load_table(fp)
            metadata = {x: {'taxonomy': x.split(';')}
                        for x in bt.ids(axis='observation')}
            bt.add_metadata(metadata, axis='observation')
            with biom_open(fp, 'w') as f:
                bt.to_hdf5(f, "woltka")

            ainfo.append(ArtifactInfo(f'Taxonomic Predictions - {rank}',
                                      'BIOM', [(fp, 'biom')]))
        else:
            errors.append(f'Table {rank} was not created, please contact '
                          'qiita.help@gmail.com for more information')

    fp_biom = f'{out_dir}/none.biom'
    if exists(fp_biom):
        ainfo.append(ArtifactInfo('Per genome Predictions', 'BIOM', [
            (fp_biom, 'biom')]))
    else:
        errors.append('Table none/per-genome was not created, please contact '
                      'qiita.help@gmail.com for more information')

    if database_gene_coordinates is not None:
        fp_biom = f'{out_dir}/per-gene.biom'
        if exists(fp_biom):
            ainfo.append(ArtifactInfo('Per gene Predictions', 'BIOM', [
                (fp_biom, 'biom')]))
        else:
            errors.append('Table per-gene was not created, please contact '
                          'qiita.help@gmail.com for more information')

    if errors:
        return False, ainfo, '\n'.join(errors)
    else:

        return True, ainfo, ""
