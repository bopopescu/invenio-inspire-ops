# -*- coding: utf-8 -*-
##
## This file is part of Invenio.
## Copyright (C) 2012 CERN.
##
## Invenio is free software; you can redistribute it and/or
## modify it under the terms of the GNU General Public License as
## published by the Free Software Foundation; either version 2 of the
## License, or (at your option) any later version.
##
## Invenio is distributed in the hope that it will be useful, but
## WITHOUT ANY WARRANTY; without even the implied warranty of
## MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
## General Public License for more details.
##
## You should have received a copy of the GNU General Public License
## along with Invenio; if not, write to the Free Software Foundation, Inc.,
## 59 Temple Place, Suite 330, Boston, MA 02111-1307, USA.

"""
Self citations task

Stores self-citations in a table for quick access

  Examples:
   (run a daemon job)
      bibrank -w selfcites
   (run on a set of records)
      selfcites -i 1-20
   (run on a collection)
      selfcites -c "Reports"

This task handles the self-citations computation
It is run on modified records so that it can update the tables used for
displaying info in the citesummary format
"""

import sys
import ConfigParser
import time
from datetime import datetime

from invenio.intbitset import intbitset
from invenio.config import CFG_BIBRANK_SELFCITES_USE_BIBAUTHORID, \
                           CFG_ETCDIR
from invenio.bibtask import task_get_option, write_message, \
                            task_sleep_now_if_required, \
                            task_update_progress
from invenio.dbquery import run_sql
from invenio.bibrank_selfcites_indexer import update_self_cites_tables, \
                                              compute_friends_self_citations, \
                                              compute_simple_self_citations, \
                                              get_authors_tags
from invenio.bibrank_citation_searcher import get_refers_to
from invenio.bibauthorid_daemon import get_user_logs as bibauthorid_user_log
from invenio.bibrank_citation_indexer import get_bibrankmethod_lastupdate
from invenio.bibrank_tag_based_indexer import intoDB, fromDB
from invenio.intbitset import intbitset


def compute_and_store_self_citations(recid, tags, citations_fun, selfcites_dic,
                                                                verbose=False):
    """Compute and store self-cites in a table

    Args:
     - recid
     - tags: used when bibauthorid is desactivated see get_author_tags()
            in bibrank_selfcites_indexer
    """
    assert recid

    if verbose:
        write_message("* processing %s" % recid)
    references = get_refers_to(recid)
    recids_to_check = set([recid]) | set(references)
    placeholders = ','.join('%s' for r in recids_to_check)
    rec_row = run_sql("SELECT MAX(`modification_date`) FROM `bibrec`"
                      " WHERE `id` IN (%s)" % placeholders, recids_to_check)

    try:
        rec_timestamp = rec_row[0]
    except IndexError:
        write_message("record not found")
        return

    cached_citations_row = run_sql("SELECT `count` FROM `rnkSELFCITES`"
               " WHERE `last_updated` >= %s"
               " AND `id_bibrec` = %s", (rec_timestamp[0], recid))
    if cached_citations_row and cached_citations_row[0][0]:
        if verbose:
            write_message("%s found (cached)" % cached_citations_row[0])
    else:
        cites = citations_fun(recid, tags)
        selfcites_dic[recid] = len(cites)
        replace_cites(recid, cites)
        sql = """REPLACE INTO rnkSELFCITES (`id_bibrec`, `count`, `references`,
                 `last_updated`) VALUES (%s, %s, %s, NOW())"""
        references_string = ','.join(str(r) for r in references)
        run_sql(sql, (recid, len(cites), references_string))
        if verbose:
            write_message("%s found" % len(cites))


def replace_cites(recid, new_cites):
    """Update database with new citations set

    Given a set of self citations:
    * stores the new ones in the database
    * removes the old ones from the database
    """
    old_cites = set(row[0] for row in run_sql("""SELECT citer
                                                  FROM rnkSELFCITEDICT
                                                  WHERE citee = %s""", [recid]))

    cites_to_add = new_cites - old_cites
    cites_to_delete = old_cites - new_cites

    for cit in cites_to_add:
        write_message('adding cite %s %s' % (recid, cit), verbose=1)
        now = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        run_sql("""INSERT INTO rnkSELFCITEDICT (citee, citer, last_updated)
                   VALUES (%s, %s, %s)""", (recid, cit, now))

    for cit in cites_to_delete:
        write_message('deleting cite %s %s' % (recid, cit), verbose=1)
        run_sql("""DELETE FROM rnkSELFCITEDICT
                   WHERE citee = %s and citer = %s""", (recid, cit))


def rebuild_tables(rank_method_code, config):
    """Rebuild the tables from scratch

    Called by bibrank -w selfcites -R
    """
    task_update_progress('emptying tables')
    empty_self_cites_tables()
    task_update_progress('filling tables')
    fill_self_cites_tables(rank_method_code, config)
    return True


def fetch_bibauthorid_last_update():
    """Fetch last runtime of bibauthorid"""
    bibauthorid_log = bibauthorid_user_log(userinfo='daemon',
                                           action='PID_UPDATE',
                                           only_most_recent=True)
    try:
        bibauthorid_end_date = bibauthorid_log[0][2]
    except IndexError:
        bibauthorid_end_date = datetime(year=1900, month=1, day=1)

    return bibauthorid_end_date.strftime("%Y-%m-%d %H:%M:%S")


def fetch_index_update():
    """Fetch last runtime of given task"""
    end_date = get_bibrankmethod_lastupdate('citation')

    if CFG_BIBRANK_SELFCITES_USE_BIBAUTHORID:
        bibauthorid_end_date = fetch_bibauthorid_last_update()
        end_date = min(end_date, bibauthorid_end_date)

    return end_date


def fetch_records(start_date, end_date):
    """Filter records not indexed out of recids

    We need to run after bibauthorid // bibrank citation indexer
    """
    sql = """SELECT `id` FROM `bibrec`
             WHERE `modification_date` <= %s
             AND `modification_date` > %s"""
    records = run_sql(sql, (end_date, start_date))
    return intbitset(records)


def fetch_concerned_records(name, ids_param):
    """Fetch records that have been updated since the last run of the daemon"""
    if ids_param:
        recids = intbitset()
        for first, last in ids_param:
            recids += range(first, last+1)
        end_date = None
    else:
        start_date = get_bibrankmethod_lastupdate(name)
        end_date = fetch_index_update()
        recids = fetch_records(start_date, end_date)
    return recids, end_date


def store_last_updated(name, date):
    """Updates method last run date"""
    run_sql("UPDATE rnkMETHOD SET last_updated=%s WHERE name=%s", (date, name))


def read_configuration(rank_method_code):
    """Loads the config file from disk and parses it"""
    filename = CFG_ETCDIR + "/bibrank/" + rank_method_code + ".cfg"
    config = ConfigParser.ConfigParser()
    try:
        config.readfp(open(filename))
    except StandardError:
        write_message("Cannot find configuration file: %s" % filename, sys.stderr)
        raise
    return config


def process_updates(rank_method_code):
    """
    This is what gets executed first when the task is started.
    It handles the --rebuild option. If that option is not specified
    we fall back to the process_one()
    """
    write_message("Running rank method: %s" % rank_method_code, verbose=0)

    selfcites_config = read_configuration(rank_method_code)
    config = {
        'algorithm': selfcites_config.get(rank_method_code, "algorithm"),
        'friends_threshold': selfcites_config.get(rank_method_code, "friends_threshold")
    }
    quick = task_get_option("quick") != "no"
    if not quick:
        return rebuild_tables(rank_method_code, config)

    tags = get_authors_tags()
    recids, end_date = fetch_concerned_records(rank_method_code,
                                               task_get_option("id"))
    citations_fun = get_citations_fun(config['algorithm'])
    selfcites_dic = fromDB(rank_method_code)

    write_message("recids %s" % str(recids))

    total = len(recids)
    for count, recid in enumerate(recids):
        task_sleep_now_if_required(can_stop_too=True)
        msg = "Extracting for %s (%d/%d)" % (recid, count + 1, total)
        task_update_progress(msg)
        write_message(msg)

        process_one(recid, tags, citations_fun, selfcites_dic)

    intoDB(selfcites_dic, end_date, rank_method_code)

    write_message("Complete")
    return True


def get_citations_fun(algorithm):
    """Returns the computation function given the algorithm name"""
    if algorithm == 'friends':
        citations_fun = compute_friends_self_citations
    else:
        citations_fun = compute_simple_self_citations
    return citations_fun


def process_one(recid, tags, citations_fun, selfcites_dic):
    """Self-cites core func, executed on each recid"""
    # First update this record then all its references
    compute_and_store_self_citations(recid, tags, citations_fun, selfcites_dic)

    references = get_refers_to(recid)
    for recordid in references:
        compute_and_store_self_citations(recordid,
                                         tags,
                                         citations_fun,
                                         selfcites_dic)


def empty_self_cites_tables():
    """
    This will empty all the self-cites tables

    The purpose is to rebuild the tables from scratch in case there is problem
    with them: inconsitencies, corruption,...
    """
    run_sql('TRUNCATE rnkSELFCITES')
    run_sql('TRUNCATE rnkEXTENDEDAUTHORS')
    run_sql('TRUNCATE rnkRECORDSCACHE')


def fill_self_cites_tables(rank_method_code, config):
    """
    This will fill the self-cites tables with data

    The purpose of this function is to fill these tables on a website that
    never ran the self-cites daemon

    This is an optimization when running on empty tables, and we hope the
    result is the same as the compute_and_store_self_citations.
    """
    begin_date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    algorithm = config['algorithm']
    tags = get_authors_tags()
    selfcites_dic = {}
    all_ids = intbitset(run_sql('SELECT id FROM bibrec ORDER BY id'))
    citations_fun = get_citations_fun(algorithm)
    write_message('using %s' % citations_fun.__name__)
    if algorithm == 'friends':
        # We only needs this table for the friends algorithm or assimilated
        # Fill intermediary tables
        for index, recid in enumerate(all_ids):
            if index % 1000 == 0:
                msg = 'intermediate %d/%d' % (index, len(all_ids))
                task_update_progress(msg)
                write_message(msg)
                task_sleep_now_if_required()
            update_self_cites_tables(recid, config, tags)
    # Fill self-cites table
    for index, recid in enumerate(all_ids):
        if index % 1000 == 0:
            msg = 'final %d/%d' % (index, len(all_ids))
            task_update_progress(msg)
            write_message(msg)
            task_sleep_now_if_required()
        compute_and_store_self_citations(recid,
                                         tags,
                                         citations_fun,
                                         selfcites_dic)
    intoDB(selfcites_dic, begin_date, rank_method_code)
