#!/usr/bin/env python3.7
""" SPARC curation cli for fetching, validating datasets, and reporting.
Usage:
    spc clone <project-id>
    spc pull [options] [<directory>...]
    spc refresh [options] [<path>...]
    spc fetch [options] [<path>...]
    spc find [options] --name=<PAT>...
    spc status [options]
    spc meta [options] [--uri] [--browser] [--human] [--diff] [<path>...]
    spc export [ttl json datasets schemas] [options]
    spc report size [options] [<path>...]
    spc report tofetch [options] [<directory>...]
    spc report terms [anatomy cells subcelluar] [options]
    spc report [completeness filetypes pathids keywords subjects samples errors test] [options]
    spc report [contributors] [options]
    spc shell [affil integration protocols] [options]
    spc server [options]
    spc tables [<directory>...]
    spc annos [export shell]
    spc feedback <feedback-file> <feedback>...
    spc missing [options]
    spc xattrs [options]
    spc demos [options]
    spc goto <remote-id>
    spc fix [options] [duplicates mismatch] [<path>...]
    spc stash [options --restore] <path>...

Commands:
    clone       clone a remote project (creates a new folder in the current directory)

    pull        retrieve remote file structure

                options: --empty

    refresh     retrieve remote file sizes and fild ids (can also fetch using the new data)

                options: --fetch
                       : --level
                       : --only-no-file-id

    fetch       fetch remote data based on local metadata (NOTE does NOT refresh first)

                options: --level

    find        list unfetched files with option to fetch

                options: --name=<PAT>...  glob options should be quoted to avoid expansion
                       : --existing       include existing files in search
                       : --refresh        refresh matching files
                       : --fetch          fetch matching files
                       : --level

    status      list existing files where local meta does not match cached

    meta        display the metadata the current folder or specified paths

                options: --diff     diff the local and cached metadata
                       : --browser  navigate to the human uri for this file
                       : --human

    export      export extracted data to json (and everything else)

                datasets        ttl for individual datasets in addition to full export
                json            json for a single dataset
                ttl             turtle for a single dataset

                options: --latest   run derived pipelines from latest json
                       : --open     open the output file using xopen

    report      print a report on all datasets

                size            dataset sizes and file counts
                completeness    submission and curation completeness
                filetypes       filetypes used across datasets
                pathids         mapping from local path to cached id
                keywords        keywords used per dataset
                terms           all ontology terms used in the export

                                anatomy
                                cells
                                subcelluar

                subjects        all headings from subjects files
                errors          list of all errors per dataset

                options: --raw  run reports on live data without export
                       : --tab-table
                       : --sort-count-desc
                       : --debug

    shell       drop into an ipython shell

                integration     integration subshell with different defaults

    server      reporting server

                options: --raw  run server on live data without export

    missing     find and fix missing metadata
    xattrs      populate metastore / backup xattrs
    demos       long running example queries
    goto        given an id cd to the containing directory
                invoke as `pushd $(spc goto <id>)`
    dedupe      find and resolve cases with multiple ids
    fix         broke something? put the code to fix it here

                mismatch
                duplicates
    stash       stash a copy of the specific files and their parents

Options:
    -f --fetch              fetch matching files
    -R --refresh            refresh matching files
    -r --rate=HZ            sometimes we can go too fast when fetching [default: 5]
    -l --limit=SIZE_MB      the maximum size to download in megabytes [default: 2]
                            use negative numbers to indicate no limit
    -L --level=LEVEL        how deep to go in a refresh
                            used by any command that acceps <path>...
    -p --pretend            if the defult is to act, dont, opposite of fetch

    -h --human              print human readable values
    -b --browser            open the uri in default browser
    -u --uri                print the human uri for the path in question
    -a --uri-api            print the api uri for the path in question
    -n --name=<PAT>         filename pattern to match (like find -name)
    -e --empty              only pull empty directories
    -x --exists             when searching include files that have already been pulled
    -m --only-meta          only pull known dataset metadata files
    -z --only-no-file-id    only pull files missing file_id
    -o --overwrite          fetch even if the file exists
    --project-path=<PTH>    set the project path manually

    -t --tab-table          print simple table using tabs for copying
    -A --latest             run further export states from the latest primary export
    -W --raw                run reporting on live data without export

    -S --sort-size-desc     sort by file size, largest first
    -C --sort-count-desc    sort by count, largest first

    -O --open               open the output file
    -U --upload             update remote target (e.g. a google sheet) if one exists
    -N --no-google          hack for ipv6 issues
    -D --diff               diff local vs cache

    --port=PORT             server port [default: 7250]

    -d --debug              drop into a shell after running a step
    -v --verbose            print extra information
    --log-location=PATH     folder into which logs are saved [default: ${SPARC_EXPORTS}/log/]
"""

import re
import sys
import csv
import json
import errno
import types
import pprint
import logging
from itertools import chain
from collections import Counter, defaultdict
import requests
import htmlfn as hfn
import ontquery as oq
from augpathlib import FileSize
from augpathlib import RemotePath, AugmentedPath  # for debug
from pyontutils import clifun as clif
from pyontutils.core import OntResGit
from pyontutils.utils import NOWDANGER, NOWISO, UTCNOWISO
from pyontutils.config import auth as pauth
from pysercomb.pyr import units as pyru
from terminaltables import AsciiTable
from sparcur import config
from sparcur import schemas as sc
from sparcur import datasets as dat
from sparcur import exceptions as exc
from sparcur.core import JT, log, logd, JPointer, lj
from sparcur.core import OntId, OntTerm, get_all_errors, DictTransformer as DT, adops
from sparcur.utils import python_identifier, want_prefixes
from sparcur.paths import Path, BlackfynnCache, PathMeta, StashPath
from sparcur.state import State
from sparcur.derives import Derives as De
from sparcur.backends import BlackfynnRemote
from sparcur.curation import PathData, Summary, Integrator, ExporterSummarizer, DatasetObject
from sparcur.curation import JEncode, TriplesExportDataset, TriplesExportSummary
from sparcur.protocols import ProtocolData
from sparcur.blackfynn_api import BFLocal
from IPython import embed


class Options(clif.Options):

    @property
    def limit(self):
        l = int(self.args['--limit'])
        if l >= 0:
            return l

    @property
    def level(self):
        return int(self.args['--level']) if self.args['--level'] else None

    @property
    def rate(self):
        return int(self.args['--rate']) if self.args['--rate'] else None


class Dispatcher(clif.Dispatcher):
    spcignore = ('.git',
                 '.~lock',)

    def _print_table(self, rows, title=None, align=None, ext=None):
        """ ext is only used when self.options.server -> True """
        def simple_tsv(rows):
            return '\n'.join('\t'.join((str(c) for c in r)) for r in rows) + '\n'

        if self.options.tab_table:
            if title:
                print(title)

            print(simple_tsv(rows))

        elif self.options.server:
            if ext is not None:
                if ext == '.tsv':
                    nowish = UTCNOWISO('seconds')
                    fn = json.dumps(f'{title} {nowish}')
                    return simple_tsv(rows), 200, {'Content-Type': 'text/tsv; charset=utf-8',
                                                   'Content-Disposition': f'attachment; filename={fn}'}
                if isinstance(ext, types.FunctionType):
                    return ext(hfn.render_table(rows[1:], *rows[0]), title=title)
                else:
                    return 'Not found', 404

            return hfn.render_table(rows[1:], *rows[0]), title

        else:
            table = AsciiTable(rows, title=title)
            if align:
                assert len(align) == len(rows[0])
                table.justify_columns = {i:('left' if v == 'l'
                                            else ('center' if v == 'c'
                                                  else ('right' if v == 'r' else 'left')))
                                         for i, v in enumerate(align)}
            print(table.table)

    def _print_paths(self, paths, title=None):
        if self.options.sort_size_desc:
            key = lambda ps: -ps[-1]
        else:
            key = lambda ps: ps

        rows = [['Path', 'size', '?'],
                *((p, s.hr if isinstance(s, FileSize) else s, 'x' if p.exists() else '')
                  for p, s in
                  sorted(([p, ('/' if p.is_dir() else
                               (p.cache.meta.size if p.cache.meta.size else '??')
                               if p.cache.meta else '_')]
                          for p in paths), key=key))]
        self._print_table(rows, title)


class Main(Dispatcher):
    child_port_attrs = ('anchor',
                        'project_path',
                        'project_id',
                        'bfl',
                        'summary',
                        'cwd',
                        'cwdintr')
    # things all children should have
    # kind of like a non optional provides you WILL have these in your namespace
    def __init__(self, options):
        super().__init__(options)
        if not self.options.verbose:
            log.setLevel('INFO')
            logd.setLevel('INFO')

        if self.options.project_path:
            self.cwd = Path(self.options.project_path).resolve()
        else:
            self.cwd = Path.cwd()

        Integrator.rate = self.options.rate
        Integrator.no_google = self.options.no_google

        self.cwdintr = Integrator(self.cwd)

        # FIXME populate this via decorator
        if (self.options.clone or
            self.options.meta or
            self.options.goto or
            self.options.tofetch or  # size does need a remote but could do it lazily
            self.options.filetypes or
            self.options.status or  # eventually this should be able to query whether there is new data since the last check
            self.options.pretend or
            (self.options.find and not (self.options.fetch or self.options.refresh))):
            # short circuit since we don't know where we are yet
            Integrator.no_google = True
            return

        elif (self.options.pull or
              self.options.mismatch or
              self.options.stash or
              self.options.contributors or
              self.options.missing):
            Integrator.no_google = True

        self._setup_local()  # if this isn't run up here the internal state of the program get's wonky

        if self.options.report and not self.options.raw:
            Integrator.setup(local_only=True)  # FIXME sigh
        else:
            self._setup_bfl()

        if self.options.export or self.options.shell:
            self._setup_export()
            self._setup_ontquery()

    def _setup_local(self):
        # pass debug along (sigh)
        AugmentedPath._debug = True
        RemotePath._debug = True
        self.BlackfynnRemote = BlackfynnCache._remote_class
        self.BlackfynnRemote._async_rate = self.options.rate

        local = self.cwd

        # we have to start from the cache class so that
        # we can configure
        try:
            _cpath = local.cache  # FIXME project vs subfolder
            if _cpath is None:
                raise exc.NoCachedMetadataError  # FIXME somehow we decided not to raise this!??!
            self.anchor = _cpath.anchor
        except exc.NoCachedMetadataError as e:
            root = local.find_cache_root()
            if root is not None:
                self.anchor = root.cache
                if local.skip_cache:
                    print(f'{local} is ignored!')
                    sys.exit(112)

                raise NotImplementedError('TODO recover meta?')
            else:
                print(f'{local} is not in a project!')
                sys.exit(111)

        self.anchor.anchorClassHere()  # replace a bottom up anchoring rule with top down
        self.project_path = self.anchor.local
        self.summary = Summary(self.project_path)
        if self.options.debug:
            Summary._debug = True

    def _setup_bfl(self):
        self.BlackfynnRemote.anchorTo(self.anchor)

        self.bfl = self.BlackfynnRemote._api
        State.bind_blackfynn(self.bfl)

    def _setup_export(self):
        #PathData.project_path = self.project_path  # FIXME bad ...
        Integrator.setup()
        ProtocolData.setup()  # FIXME this suggests that we need a more generic setup file than this cli

    def _setup_ontquery(self):
        # FIXME this should be in its own setup method
        # pull in additional graphs for query that aren't loaded properly
        RDFL = oq.plugin.get('rdflib')
        olr = Path(pauth.get_path('ontology-local-repo'))
        branch = 'methods'
        for fn in ('methods', 'methods-helper', 'methods-core'):
            org = OntResGit(olr / f'ttl/{fn}.ttl', ref=branch)
            OntTerm.query.ladd(RDFL(org.graph, OntId))

    @property
    def project_name(self):
        return self.anchor.name
        #return self.bfl.organization.name

    @property
    def project_id(self):
        #self.bfl.organization.id
        return self.anchor.id

    @property
    def datasets(self):
        yield from self.anchor.children  # ok to yield from cache now that it is the bridge

    @property
    def datasets_remote(self):
        for d in self.anchor.remote.children:
            # FIXME lo the crossover (good for testing assumptions ...)
            #yield d.local
            yield d

    @property
    def datasets_local(self):
        for d in self.anchor.local.children: #self.datasets:
            if d.exists():
                yield d

    ###
    ## vars
    ###

    @property
    def directories(self):
        return [Path(string_dir).absolute() for string_dir in self.options.directory]

    @property
    def paths(self):
        return [Path(string_path).absolute() for string_path in self.options.path]

    @property
    def _paths(self):
        """ all relevant paths determined by the flags that have been set """
        # but if you use the generator version of _paths
        # then if you add a folder to the previous path
        # then it will yeild that folder! which is SUPER COOL
        # but breaks lots of asusmptions elsehwere
        paths = self.paths
        if not paths:
            paths = self.cwd,  # don't call Path.cwd() because it may have been set from --project-path

        if self.options.only_meta:
            paths = (mp.absolute() for p in paths for mp in dat.DatasetStructureLax(p).meta_paths)
            yield from paths
            return

        yield from self._build_paths(paths)

    def _build_paths(self, paths):
        def inner(paths, level=0, stop=self.options.level):
            """ depth first traversal of children """
            for path in paths:
                if self.options.only_no_file_id:
                    if (path.is_broken_symlink() and
                        (path.cache.meta.file_id is None)):
                        yield path
                        continue

                elif self.options.empty:
                    if path.is_dir():
                        try:
                            next(path.children)
                            # if a path has children we still want to
                            # for empties in them to the level specified
                        except StopIteration:
                            yield path
                    else:
                        continue
                else:
                    yield path

                if stop is None:
                    if self.options.only_no_file_id:
                        for rc in path.rchildren:
                            if (rc.is_broken_symlink() and
                                rc.cache.meta.file_id is None):
                                yield rc
                    else:
                        yield from path.rchildren

                elif level <= stop:
                    yield from inner(path.children, level + 1)

        yield from inner(paths)

    @property
    def _dirs(self):
        for p in self._paths:
            if p.is_dir():
                yield p

    @property
    def _not_dirs(self):
        for p in self._paths:
            if not p.is_dir():
                yield p

    def clone(self):
        project_id = self.options.project_id
        if project_id is None:
            print('no remote project id listed')
            sys.exit(4)
        # given that we are cloning it makes sense to _not_ catch a connection error here
        self.BlackfynnRemote = BlackfynnRemote._new(Path, BlackfynnCache)
        try:
            self.BlackfynnRemote.init(project_id)
        except exc.MissingSecretError:
            print(f'missing api secret entry for {project_id}')
            sys.exit(11)

        # make sure that we aren't in a project already
        existing_root = self.cwd.find_cache_root()
        if existing_root is not None:
            message = f'fatal: already in project located at {root.resolve()!r}'
            print(message)
            sys.exit(3)

        try:
            anchor = self.BlackfynnRemote.dropAnchor(self.cwd)
        except exc.DirectoryNotEmptyError:
            message = f'fatal: destination path {anchor} already exists and is not an empty directory.'
            print(message)
            sys.exit(2)
        except BaseException as e:
            log.exception(e)
            sys.exit(11111)

        anchor.local_data_dir.mkdir()
        anchor.local_objects_dir.mkdir()
        anchor.trash.mkdir()

        self.anchor = anchor
        self.project_path = self.anchor.local
        with anchor:
            self.cwd = Path.cwd()  # have to update self.cwd so pull sees the right thing
            self.pull()

    def pull(self):
        # TODO folder meta -> org
        only = tuple()
        recursive = self.options.level is None  # FIXME we offer levels zero and infinite!
        dirs = self.directories
        cwd = self.cwd
        if self.project_path.parent.name == 'big':
            skip = self.skip
            only = self.skip_big
        else:
            skip = self.skip_big + self.skip

        if not dirs:
            dirs = cwd,

        dirs = sorted(dirs, key=lambda d: d.name)

        existing_locals = set(rc for d in dirs for rc in d.rchildren)
        # FIXME don't parse the fucking dates unless someone needs them you idiot
        existing_d = {c.cache.id:c for c in existing_locals if c.cache is not None}  # yay null cache
        existing_ids = set(existing_d)

        log.debug(dirs)
        for d in dirs:
            if self.options.empty:
                if list(d.children):
                    continue

            if not d.is_dir():
                raise TypeError(f'dir is not a dir?!? {d}')

            if not (d.remote.is_dataset() or d.remote.is_organization()):
                log.warning('You are pulling recursively from below dataset level.')

            #r = d.remote
            # FIXME for some reason this does not seem to be working as expected
            # because new datasets are being added when there is an existing dataset
            #r.refresh(update_cache=True)  # if the parent folder has moved make sure to move it first
            c = d.cache
            newc = c.refresh()  # this does the move for us now
            if newc is None:
                continue  # directory was deleted

            if d.cache.is_organization():  # FIXME FIXME FIXME hack to mask broken bootstrap handling of existing dirs :/
                for cd in d.children:
                    if cd.is_dir():
                        oc = cd.cache
                        if oc is None and cd.skip_cache:
                            continue

                        nc = oc.refresh()  # FIXME can't we just build an index off datasets here??
                        if nc != oc:
                            log.info(f'Dataset moved!\n{oc} -> {nc}')
                            # FIXME FIXME FIXME
                            with open(self.anchor.local_data_dir / 'renames.log', 'at') as f:
                                f.write(f'{oc} -> {nc} -> {nc.id}\n')

            # FIXME something after this point is retaining stale filepaths on dataset rename ...
            #d = r.local  # in case a folder moved
            caches = newc.remote.bootstrap(recursive=recursive, only=only, skip=skip)

        new_locals = set(c.local for c in caches if c is not None)  # FIXME 
        new_ids = {c.id:c for c in caches if c is not None}
        maybe_removed_ids = set(existing_ids) - set(new_ids)
        maybe_new_ids = set(new_ids) - set(existing_ids)
        if maybe_removed_ids:
            # FIXME pull sometimes has fake file extensions
            from pyontutils.utils import Async, deferred
            from pathlib import PurePath
            maybe_removed = [existing_d[id] for id in maybe_removed_ids]
            maybe_removed_stems = {PurePath(p.parent) / p.stem:p for p in maybe_removed}  # FIXME still a risk of collisions?
            maybe_new = [new_ids[id] for id in maybe_new_ids]
            maybe_new_stems = {PurePath(p.parent) / p.stem:p for p in maybe_new}
            for pstem, p in maybe_new_stems.items():
                if pstem in maybe_removed_stems:
                    mr_path = maybe_removed_stems[pstem]
                    #assert p != mr_path, f'wat\n{mr_path}\n{p}'
                    if p != mr_path:
                        new_new_path = p.refresh()
                    else:
                        new_new_path = p
                        # TODO check if file_id needs to be updated in some cases ...
                        # csv files match
                        log.info(f'wat\n{mr_path}\n{p}')

                    if new_new_path == mr_path:
                        maybe_removed.remove(mr_path)

            Async(rate=self.options.rate)(deferred(l.cache.refresh)() for l in maybe_removed
                                          # FIXME deal with untracked files
                                          if l.cache)

    ###
    skip = (
            'N:dataset:83e0ebd2-dae2-4ca0-ad6e-81eb39cfc053',  # hackathon
            'N:dataset:a896935a-4718-4906-8a7b-b6d76fb260b6',  # test computational resource
            'N:dataset:8bcf659c-f4b3-425f-ac33-8c560e02d4aa',  # test dataset
        )

    skip_big = (
            'N:dataset:ec2e13ae-c42a-4606-b25b-ad4af90c01bb',  # big max
            'N:dataset:2d0a2996-be8a-441d-816c-adfe3577fc7d',  # big rna
            'N:dataset:ca9afa19-b616-41a9-a532-3ae5aaf4088f',  # big tif
            #'N:dataset:a7b035cf-e30e-48f6-b2ba-b5ee479d4de3',  # powley done
        )
    ###

    def refresh(self):
        paths = self.paths
        cwd = self.cwd
        if not paths:
            paths = cwd,

        to_root = sorted(set(parent
                             for path in paths
                             for parent in path.parents
                             if parent.cache is not None),
                         key=lambda p: len(p.parts))

        if self.options.pretend:
            ap = list(chain(to_root, self._paths))
            self._print_paths(ap)
            print(f'total = {len(ap):<10}rate = {self.options.rate}')
            return

        self._print_paths(chain(to_root, self._paths))

        from pyontutils.utils import Async, deferred
        hz = self.options.rate
        fetch = self.options.fetch
        limit = self.options.limit

        drs = [d.remote for d in chain(to_root, self._dirs)]

        if not self.options.debug:
            refreshed = Async(rate=hz)(deferred(r.refresh)(update_data_on_cache=r.cache.is_file() and
                                                           r.cache.exists()) for r in drs)
        else:
            refreshed = [r.refresh(update_data_on_cache=r.cache.is_file() and
                                   r.cache.exists()) for r in drs]

        moved = []
        parent_moved = []
        for new, r in zip(refreshed, drs):
            if new is None:
                log.critical('utoh')

            oldl = r.local
            try:
                r.update_cache()  # calling this directly is ok for directories
            except FileNotFoundError as e:
                parent_moved.append(oldl)
                continue
            except OSError as e:
                if e.errno == errno.ENOTEMPTY:
                    log.error(f'{e}')
                    continue
                else:
                    raise e

            newl = r.local
            if oldl != newl:
                moved.append([oldl, newl])

        if moved:
            self._print_table(moved, title='Folders moved')
            for old, new in moved:
                if old == cwd:
                    log.info(f'Changing directory to {new}')
                    new.chdir()

        if parent_moved:
            self._print_paths(parent_moved, title='Parent moved')

        if not self.options.debug:
            refreshed = Async(rate=hz)(deferred(path.cache.refresh)(update_data=fetch,
                                                                    size_limit_mb=limit)
                                       for path in self._not_dirs)

        else:
            for path in self._not_dirs:
                path.cache.refresh(update_data=fetch, size_limit_mb=limit)

    def fetch(self):
        paths = [p for p in self._paths if not p.is_dir()]
        self._print_paths(paths)
        if self.options.pretend:
            return

        from pyontutils.utils import Async, deferred
        hz = self.options.rate
        Async(rate=hz)(deferred(path.cache.fetch)(size_limit_mb=self.options.limit)
                       for path in paths)

    @property
    def export_base(self):
        return self.project_path.parent / 'export' / self.project_id

    @property
    def LATEST(self):
        return self.project_path.parent / 'export' / self.project_id / 'LATEST'

    @property
    def latest_export(self):
        with open(self.LATEST / 'curation-export.json', 'rt') as f:
            return json.load(f)

    def latest_export_ttl_populate(self, graph):
        # intentionally fail if the ttl export failed
        return graph.parse((self.LATEST / 'curation-export.ttl').as_posix(), format='ttl')

    def export(self):
        """ export output of curation workflows to file """
        #org_id = Integrator(self.project_path).organization.id

        if self.options.schemas:
            schemas = (sc.DatasetDescriptionSchema,
                       sc.SubjectsSchema,
                       sc.SamplesFileSchema,
                       sc.SubmissionSchema,)

            sb = self.project_path.parent / 'export' / 'schemas'  # FIXME run without having to be in a project
            for s in schemas:
                s.export(sb)

            return

        cwd = self.cwd
        timestamp = NOWDANGER(implicit_tz='PST PDT')
        format_specified = self.options.ttl or self.options.json  # This is OR not XOR you dumdum
        if cwd != cwd.cache.anchor and format_specified:
            if not cwd.cache.is_dataset:
                print(f'{cwd.cache} is not at dataset level!')
                sys.exit(123)

            intr = Integrator(cwd)
            dump_path = self.export_base / 'datasets' / intr.id / timestamp
            latest_path = self.export_base / 'datasets' / intr.id / 'LATEST'
            if not dump_path.exists():
                dump_path.mkdir(parents=True)

            functions = []
            suffixes = []
            modes = []
            if self.options.json:  # json first since we can cache dowe
                j = lambda f: json.dump(intr.data, f,
                                        sort_keys=True, indent=2, cls=JEncode)
                functions.append(j)
                suffixes.append('.json')
                modes.append('wt')

            if self.options.ttl:
                t = lambda f: f.write(intr.ttl)
                functions.append(t)
                suffixes.append('.ttl')
                modes.append('wb')

            filename = 'curation-export'
            filepath = dump_path / filename

            for function, suffix, mode in zip(functions, suffixes, modes):
                out = filepath.with_suffix(suffix)
                with open(out, mode) as f:
                    function(f)

                log.info(f'dataset graph exported to {out}')

                if self.options.open:
                    out.xopen()

            if latest_path.exists():
                if not latest_path.is_symlink():
                    raise TypeError(f'Why is LATEST not a symlink? {latest_path!r}')

                latest_path.unlink()

            latest_path.symlink_to(dump_path)

            return

        # start time not end time ...
        # obviously not transactional ...
        filename = 'curation-export'
        dump_path = self.export_base / timestamp
        latest_path = self.LATEST
        if not dump_path.exists():
            dump_path.mkdir(parents=True)

        filepath = dump_path / filename

        data = self.latest_export if self.options.latest else self.summary.data

        # FIXME we still create a new export folder every time even if the json didn't change ...
        with open(filepath.with_suffix('.json'), 'wt') as f:
            json.dump(data, f, sort_keys=True, indent=2, cls=JEncode)

        es = ExporterSummarizer(data)

        with open(filepath.with_suffix('.ttl'), 'wb') as f:
            f.write(es.ttl)

        for xml_name, xml in es.xml:
            with open(filepath.with_suffix(f'.{xml_name}.xml'), 'wb') as f:
                f.write(xml)

        # datasets, contributors, subjects, samples, resources
        for table_name, tabular in es.disco:
            with open(filepath.with_suffix(f'.{table_name}.tsv'), 'wt') as f:
                writer = csv.writer(f, delimiter='\t', lineterminator='\n')
                writer.writerows(tabular)

        if self.options.datasets:
            dataset_dump_path = dump_path / 'datasets'
            dataset_dump_path.mkdir()
            suffix = '.ttl'
            mode = 'wb'
            for dataset_blob in es:
                filepath = dataset_dump_path / dataset_blob['id']
                out = filepath.with_suffix(suffix)
                with open(out, 'wb') as f:
                    f.write(TriplesExportDataset(dataset_blob).ttl)

                log.info(f'dataset graph exported to {out}')

        if latest_path.exists():
            if not latest_path.is_symlink():
                raise TypeError(f'Why is LATEST not a symlink? {latest_path!r}')

            latest_path.unlink()

        latest_path.symlink_to(dump_path)

        if self.options.debug:
            embed()

    def annos(self):
        from protcur.analysis import protc, Hybrid
        from sparcur.protocols import ProtcurSource
        ProtcurSource.populate_annos()
        if self.options.export:
            with open('/tmp/sparc-protcur.rkt', 'wt') as f:
                f.write(protc.parsed())

        all_blackfynn_uris = set(u for d in self.summary for u in d.protocol_uris_resolved)
        all_hypotehsis_uris = set(a.uri for a in protc)
        if self.options.shell or self.options.debug:
            p, *rest = self._paths
            f = Integrator(p)
            all_annos = [list(protc.byIri(uri)) for uri in f.protocol_uris_resolved]
            embed()

    def demos(self):
        # get the first dataset
        dataset = next(iter(summary))

        # another way to get the first dataset
        dataset_alt = next(org.children)

        # view all dataset descriptions call repr(tabular_view_demo)
        tabular_view_demo = [next(d.dataset_description).t
                                for d in ds[:1]
                                if 'dataset_description' in d.data]

        # get package testing
        bigskip = ['N:dataset:2d0a2996-be8a-441d-816c-adfe3577fc7d',
                    'N:dataset:ec2e13ae-c42a-4606-b25b-ad4af90c01bb']
        bfds = self.bfl.bf.datasets()
        packages = [list(d.packages) for d in bfds[:3]
                    if d.id not in bigskip]
        n_packages = [len(ps) for ps in packages]

        # bootstrap a new local mirror
        # FIXME at the moment we can only have of these at a time
        # sigh more factories incoming
        #anchor = BlackfynnCache('/tmp/demo-local-storage')
        #anchor.bootstrap()

        if False:
            ### this is the equivalent of export, quite slow to run
            # export everything
            dowe = summary.data

            # show all the errors from export everything
            error_id_messages = [(d['id'], e['message']) for d in dowe['datasets'] for e in d['errors']]
            error_messages = [e['message'] for d in dowe['datasets'] for e in d['errors']]

        #rchilds = list(datasets[0].rchildren)
        #package, file = [a for a in rchilds if a.id == 'N:package:8303b979-290d-4e31-abe5-26a4d30734b4']

        return self.shell()

    def tables(self):
        """ print summary view of raw metadata tables, possibly per dataset """
        # TODO per dataset
        from sparcur.datasets import DatasetStructure
        from sparcur import pipelines as pipes
        DatasetStructure._refresh_on_missing = False
        summary = self.summary
        tables = []
        datasets = self.summary.iter_datasets if self.cwd.cache.is_organization() else (Integrator(self.cwd.cache.dataset.local),)
        for intr in datasets:
            pipe = intr.pipeline
            while not isinstance(pipe, pipes.SPARCBIDSPipeline):
                pipe = pipe.previous_pipeline

            try:
                pipe.subpipelined
            except pipe.SkipPipelineError:
                continue

            for sp in pipe.subpipeline_instances:
                tabular = sp._transformer.t
                tables.append(tabular)

        [print(repr(t)) for t in tables]
        return
        tabular_view_demo = [next(d.dataset_description).t
                                for d in summary
                                if 'dataset_description' in d.data]
        print(repr(tabular_view_demo))

    def find(self):
        paths = []
        if self.options.name:
            patterns = self.options.name
            path = self.cwd
            for pattern in patterns:
                # TODO filesize mismatches on non-fake
                # no longer needed due to switching to symlinks
                #if '.fake' not in pattern and not self.options.overwrite:
                    #pattern = pattern + '.fake*'

                for file in path.rglob(pattern):
                    paths.append(file)

        if paths:
            paths = [p for p in paths if not p.is_dir()]
            search_exists = self.options.exists
            if self.options.limit:
                old_paths = paths
                paths = [p for p in paths
                         if p.cache.meta.size is None or  # if we have no known size don't limit it
                         search_exists or
                         not p.exists() and p.cache.meta.size.mb < self.options.limit
                         or p.exists() and p.size != p.cache.meta.size and
                         (not log.info(f'Truncated transfer detected for {p}\n'
                                       f'{p.size} != {p.cache.meta.size}'))
                         and p.cache.meta.size.mb < self.options.limit]

                n_skipped = len(set(p for p in old_paths if p.is_broken_symlink()) - set(paths))

            if self.options.pretend:
                self._print_paths(paths)
                print(f'skipped = {n_skipped:<10}rate = {self.options.rate}')
                return

            if self.options.verbose:
                for p in paths:
                    print(p.cache.meta.as_pretty(pathobject=p))

            if self.options.fetch or self.options.refresh:
                from pyontutils.utils import Async, deferred
                hz = self.options.rate  # was 30
                limit = self.options.limit
                fetch = self.options.fetch
                if self.options.refresh:
                    Async(rate=hz)(deferred(path.remote.refresh)
                                   (update_cache=True, update_data=fetch, size_limit_mb=limit)
                                   for path in paths)
                elif fetch:
                    Async(rate=hz)(deferred(path.cache.fetch)(size_limit_mb=limit)
                                   for path in paths)

            else:
                self._print_paths(paths)
                print(f'skipped = {n_skipped:<10}rate = {self.options.rate}')

    def feedback(self):
        file = self.options.feedback_file
        feedback = ' '.join(self.options.feedback)
        path = Path(file).resolve()
        eff = Integrator(path)
        # TODO pagenote and/or database
        print(eff, feedback)

    def missing(self):
        for path in self._paths:
            for rc in path.rchildren:
                if rc.is_broken_symlink():
                    m = rc.cache.meta
                    if m.file_id is None:
                        #print(rc)
                        print(m.as_pretty(pathobject=rc))
        #self.bfl.find_missing_meta()

    def xattrs(self):
        raise NotImplementedError('This used to populate the metastore, '
                                  'but currently does nothing.')

    def meta(self):
        if self.options.browser:
            import webbrowser

        BlackfynnCache._local_class = Path  # since we skipped _setup
        Path._cache_class = BlackfynnCache
        paths = self.paths
        if not paths:
            paths = self.cwd,

        old_level = log.level
        if not self.options.verbose:
            log.setLevel('ERROR')
        def inner(path):
            if self.options.uri or self.options.browser:
                uri = path.cache.uri_human
                print('+' + '-' * (len(uri) + 2) + '+')
                print(f'| {uri} |')
                if self.options.browser:
                    webbrowser.open(uri)

            try:
                cmeta = path.cache.meta
                if cmeta is not None:
                    if self.options.diff:
                        if cmeta.checksum is None:
                            if not path.cache.local_object_cache_path.exists():
                                # we are going to have to go to network
                                self._setup()

                            cmeta.checksum = path.cache.checksum()  # FIXME :/
                        lmeta = path.meta
                        # id and file_id are fake in this instance
                        setattr(lmeta, 'id', None)
                        setattr(lmeta, 'file_id', None)
                        print(lmeta.as_pretty_diff(cmeta, pathobject=path, human=self.options.human))
                    else:
                        print(cmeta.as_pretty(pathobject=path, human=self.options.human))

            except exc.NoCachedMetadataError:
                print(f'No metadata for {path}. Run `spc refresh {path}`')

        for path in paths:
            inner(path)

        log.setLevel(old_level)

    def goto(self):
        # TODO this needs an inverted index
        for rc in self.cwd.rchildren:
            try:
                if rc.cache.id == self.options.remote_id:
                    if rc.is_broken_symlink() or rc.is_file():
                        rc = rc.parent

                    print(rc.relative_to(self.cwd).as_posix())
                    return
            except AttributeError as e:
                if not rc.skip_cache:
                    log.critical(rc)
                    log.error(e)

    def status(self):
        project_path = self.cwd.find_cache_root()
        if project_path is None:
            print(f'{self.cwd} is not in a project!')
            sys.exit(111)

        existing_files = [f for f in project_path.rchildren if f.is_file()]
        different = []
        for f in existing_files:
            try:
                cmeta = f.cache.meta
            except AttributeError:
                if f.skip_cache:
                    continue

            lmeta = f.meta if cmeta.checksum else f.meta_no_checksum
            # id and file_id are fake in this instance
            setattr(lmeta, 'id', None)
            setattr(lmeta, 'file_id', None)
            if lmeta.content_different(cmeta):
                if self.options.status:
                    print(lmeta.as_pretty_diff(cmeta, pathobject=f, human=self.options.human))
                else:
                    yield f, lmeta, cmeta

    def server(self):
        from sparcur.server import make_app
        if self.options.raw:
            # FIXME ...
            self.dataset_index = {d.meta.id:Integrator(d) for d in self.datasets}
        else:
            data = self.latest_export
            self.dataset_index = {d['id']:d for d in data['datasets']}

        report = Report(self)
        app, *_ = make_app(report, self.project_path)
        self.app = app  # debug only
        app.debug = False
        app.run(host='localhost', port=self.options.port, threaded=True)

    def stash(self, paths=None, stashmetafunc=lambda v:v):
        if paths is None:
            paths = self.paths

        stash_base = self.anchor.local.parent / 'stash'  # TODO move to operations
        to_stash = sorted(set(parent for p in paths for parent in
                               chain((p,), p.relative_to(self.anchor).parents)),
                           key=lambda p: len(p.as_posix()))

        if self.options.restore:
            # horribly inefficient, maybe build on a default dict?
            rcs = sorted((c for c in stash_base.rchildren if not c.is_dir()), key=lambda p:p.as_posix(), reverse=True)
            for path in paths:
                relpath = path.relative_to(self.anchor)
                for p in rcs:
                    if p.parts[-len(relpath.parts):] == relpath.parts:
                        p.copy_to(path, force=True, copy_cache_meta=True)  # FIXME old remote may have been deleted, worth a check?
                        # TODO checksum? sync?
                        break

            breakpoint()

        else:
            timestamp = NOWDANGER(implicit_tz='PST PDT')
            stash = StashPath(stash_base, timestamp)
            new_anchor = stash / self.anchor.name
            new_anchor.mkdir(parents=True)
            new_anchor.cache_init(self.anchor.meta, anchor=True)
            for p in to_stash[1:]:
                p = p.relative_to(self.anchor) if p.root == '/' else p
                new_path = new_anchor / p
                log.debug(p)
                log.debug(new_path)
                p = self.anchor.local / p
                if p.is_dir():
                    new_path.mkdir()
                    new_path.cache_init(p.cache.meta)
                else:
                    # TODO search existing stashes to see if
                    # we already have a stash of the file
                    log.debug(f'{p!r} {new_path!r}')
                    new_path.copy_from(p)
                    pc, npc = p.checksum(), new_path.checksum()
                    # TODO a better way to do this might be to
                    # treat the stash as another local for which
                    # the current local is the remote
                    # however this might require layering
                    # remote and remote remote metadata ...
                    assert pc == npc, f'\n{pc}\n{npc}'
                    cmeta = p.cache.meta
                    nmeta = stashmetafunc(cmeta)
                    log.debug(nmeta)
                    new_path.cache_init(nmeta)

            nall = list(new_anchor.rchildren)
            return nall

    ### sub dispatchers

    def report(self):
        report = Report(self)
        report()

    def shell(self):
        """ drop into an shell with classes loaded """
        shell = Shell(self)
        shell()

    def fix(self):
        fix = Fix(self)
        fix()


class Report(Dispatcher):

    paths = Main.paths
    _paths = Main._paths

    export_base = Main.export_base
    LATEST = Main.LATEST
    latest_export = Main.latest_export
    latest_export_ttl_populate = Main.latest_export_ttl_populate

    _print_table = Main._print_table

    @property
    def _sort_key(self):
        if self.options.sort_count_desc:
            return lambda kv: -kv[-1]
        else:
            return lambda kv: kv

    def contributors(self, ext=None):
        data = self.summary.data if self.options.raw else self.latest_export
        datasets = data['datasets']
        unique = {c['id']:c for d in datasets
                  if 'contributors' in d
                  for c in d['contributors']}
        contribs = sorted(unique.values(),
                          key=lambda c: c['last_name'] if 'last_name' in c else c['name'])
        #contribs = sorted((dict(c) for c in
                           #set(frozenset({k:tuple(v) if isinstance(v, list) else
                                          #(frozenset(v.items()) if isinstance(v, dict) else v)
                                          #for k, v in c.items()}.items())
                               #for d in datasets
                               #if 'contributors' in d
                               #for c in d['contributors']
                               #if not log.info(lj(c)))),
                          #key=lambda c: c['last_name'] if 'last_name' in c else c['name'])
        rows = [['id', 'last', 'first', 'PI', 'No Orcid']] + [[
            c['id'],
            c['last_name'],
            c['first_name'],
            'x' if 'contributor_role' in c and 'PrincipalInvestigator' in c['contributor_role'] else '',
            'x' if 'orcid' not in c['id'] else '']
            for c in contribs]

        return self._print_table(rows, title='Contributors Report', ext=ext)

    def tofetch(self, dirs=None):
        if dirs is None:
            dirs = self.options.directory
            if not dirs:
                dirs.append(self.cwd.as_posix())

        data = []

        def dead(p):
            raise ValueError(p)

        for d in dirs:
            if not Path(d).is_dir():
                continue  # helper files at the top level, and the symlinks that destory python
            path = Path(d).resolve()
            paths = path.rchildren #list(path.rglob('*'))
            path_meta = {p:p.cache.meta if p.cache else dead(p) for p in paths
                         if p.suffix not in ('.swp',)}
            outstanding = 0
            total = 0
            tf = 0
            ff = 0
            td = 0
            uncertain = False  # TODO
            for p, m in path_meta.items():
                #if p.is_file() and not any(p.stem.startswith(pf) for pf in self.spcignore):
                if p.is_file() or p.is_broken_symlink():
                    s = m.size
                    if s is None:
                        uncertain = True
                        continue

                    tf += 1
                    if s:
                        total += s

                    #if '.fake' in p.suffixes:
                    if p.is_broken_symlink():
                        ff += 1
                        if s:
                            outstanding += s

                elif p.is_dir():
                    td += 1

            data.append([path.name,
                         FileSize(total - outstanding),
                         FileSize(outstanding),
                         FileSize(total),
                         uncertain,
                         (tf - ff),
                         ff,
                         tf,
                         td])

        formatted = [[n, l.hr, o.hr, t.hr if not u else '??', lf, ff, tf, td]
                     for n, l, o, t, u, lf, ff, tf, td in
                     sorted(data, key=lambda r: (r[4], -r[3]))]
        rows = [['Folder', 'Local', 'To Retrieve', 'Total', 'L', 'R', 'T', 'TD'],
                *formatted]

        return self._print_table(rows, title='File size counts', ext=ext)

    def filetypes(self, ext=None):
        key = self._sort_key
        paths = self.paths if self.paths else (self.cwd,)
        paths = [c for p in paths for c in p.rchildren if not c.is_dir()]
        rex = re.compile('^\.[0-9][0-9][0-9A-Z]$')
        rex_paths = [p for p in paths if re.match(rex, p.suffix)]
        paths = [p for p in paths if not re.match(rex, p.suffix)]

        def count(thing):
            return sorted([(k if k else '', v) for k, v in
                            Counter([getattr(f, thing)
                                     for f in paths]).items()], key=key)

        each = {t:count(t) for t in ('suffix', 'mimetype', '_magic_mimetype')}
        each['suffix'].append((rex.pattern, len(rex_paths)))

        for title, rows in each.items():
            yield self._print_table(((title, 'count'), *rows), title=title.replace('_', ' ').strip())

        all_counts = sorted([(*[m if m else '' for m in k], v) for k, v in
                                Counter([(f.suffix, f.mimetype, f._magic_mimetype)
                                        for f in paths]).items()], key=key)

        header = ['suffix', 'mimetype', 'magic mimetype', 'count']
        return self._print_table((header, *all_counts),
                                 title='All types aligned (has duplicates)',
                                 ext=ext)

    def samples(self, ext=None):
        data = self.summary.data if self.options.raw else self.latest_export
        datasets = data['datasets']
        key = self._sort_key
        # FIXME we need the blob wrapper in addition to the blob generator
        # FIXME these are the normalized ones ...
        samples_headers = tuple(k for dataset_blob in datasets
                                 if 'samples' in dataset_blob  # FIXME inputs?
                                 for samples_blob in dataset_blob['samples']
                                 for k in samples_blob)
        counts = tuple(kv for kv in sorted(Counter(samples_headers).items(),
                                            key=key))

        rows = ((f'Column Name unique = {len(counts)}', '#'), *counts)
        return self._print_table(rows, title='Samples Report', ext=ext)

    def subjects(self, ext=None):
        data = self.summary.data if self.options.raw else self.latest_export
        datasets = data['datasets']
        key = self._sort_key
        # FIXME we need the blob wrapper in addition to the blob generator
        # FIXME these are the normalized ones ...
        subjects_headers = tuple(k for dataset_blob in datasets
                                 if 'subjects' in dataset_blob  # FIXME inputs?
                                 for subject_blob in dataset_blob['subjects']
                                 for k in subject_blob)
        counts = tuple(kv for kv in sorted(Counter(subjects_headers).items(),
                                            key=key))

        rows = ((f'Column Name unique = {len(counts)}', '#'), *counts)
        return self._print_table(rows, title='Subjects Report', ext=ext)

    def completeness(self, ext=None):
        if self.options.raw:
            raw = self.summary.completeness
        else:
            datasets = self.latest_export['datasets']
            raw = [self.summary._completeness(data) for data in datasets]

        def rformat(i, si, ci, ei, name, id, award, organ):
            if self.options.server and isinstance(ext, types.FunctionType):
                rsurl = 'https://projectreporter.nih.gov/reporter_searchresults.cfm'
                dataset_dash_url = self.url_for('route_datasets_id', id=id)
                errors_url = self.url_for('route_reports_errors_id', id=id)
                si = hfn.atag(errors_url + '#submission', si)
                ci = hfn.atag(errors_url + '#curation', ci)
                ei = hfn.atag(errors_url + '#total', ei)
                name = hfn.atag(dataset_dash_url, name)
                id = hfn.atag(dataset_dash_url, id[:10] + '...')
                award = hfn.atag(('https://scicrunch.org/scicrunch/data/source/'
                                  f'nif-0000-10319-1/search?q={award}'), award) if award else 'MISSING'
                organ = organ if organ else ''
                if isinstance(organ, list) or isinstance(organ, tuple):
                    organ = ' '.join([o.atag() for o in organ])
                if isinstance(organ, OntTerm):
                    organ = organ.atag()
            else:
                award = award if award else ''
                organ = (repr(organ) if isinstance(organ, OntTerm) else organ) if organ else ''
                if isinstance(organ, list):
                    organ = ' '.join([repr(o) for o in organ])
                    

            return (i + 1, si, ci, ei, name, id, award, organ)

        rows = [('', 'SI', 'CI', 'EI', 'name', 'id', 'award', 'organ')]
        rows += [rformat(i, *cols) for i, cols in
                 enumerate(sorted(raw, key=lambda t: (t[0], t[1], t[5] if t[5] else 'z' * 999, t[3])))]

        return self._print_table(rows, title='Completeness Report', ext=ext)

    def keywords(self, ext=None):
        data = self.summary.data if self.options.raw else self.latest_export
        datasets = data['datasets']
        _rows = [sorted(set(dataset_blob.get('meta', {}).get('keywords', [])), key=lambda v: -len(v))
                    for dataset_blob in datasets]
        rows = [list(r) for r in sorted(set(tuple(r) for r in _rows if r),
                                        key = lambda r: (len(r), tuple(len(c) for c in r if c), r))]
        header = [[f'{i + 1}' for i, _ in enumerate(rows[-1])]]
        rows = header + rows
        return self._print_table(rows, title='Keywords Report')

    def size(self, dirs=None, ext=None):
        if dirs is None:
            dirs = self.paths
        intrs = [Integrator(p) for p in dirs]
        if not intrs:
            intrs = self.cwdintr,

        rows = [['path', 'id', 'dirs', 'files', 'size', 'hr'],
                *sorted([[d.name, d.id, c['dirs'], c['files'], c['size'], c['size'].hr]
                         for d in intrs
                         for c in (d.datasetdata.counts,)], key=lambda r: -r[-2])]

        return self._print_table(rows, title='Size Report', align=['l', 'l', 'r', 'r', 'r', 'r'], ext=ext)

    def test(self, ext=None):
        rows = [['hello', 'world'], [1, 2]]
        return self._print_table(rows, title='Report Test', ext=ext)

    def errors(self, *, id=None, ext=None):
        if self.options.raw:
            self.summary.data['datasets']
        else:
            datasets = self.latest_export['datasets']

        if self.cwd != self.anchor:
            id = self.cwd.cache.dataset.id
            
        if id is not None:
            if not id.startswith('N:dataset:'):
                return []

            def pt(rendered_table, title=None):
                """ passthrough """
                return rendered_table

            import htmlfn as hfn
            for dataset_blob in datasets:
                if dataset_blob['id'] == id:
                    dso = DatasetObject.from_json(dataset_blob)
                    title = f'Errors for {id}'
                    urih = dataset_blob['meta']['uri_human']
                    formatted_title = (hfn.h2tag(f'Errors for {hfn.atag(urih, id)}<br>\n') +
                                       (hfn.h3tag(dataset_blob['meta']['title']
                                        if 'title' in dataset_blob['meta'] else
                                        dataset_blob['meta']['folder_name'])))
                    log.info(list(dataset_blob.keys()))
                    errors = list(dso.errors)
                    return [(self._print_table(e.as_table(), ext=pt)) for e in errors], formatted_title, title
        else:
            pprint.pprint(sorted([(d['meta']['folder_name'], [e['message']
                                                              for e in get_all_errors(d)])
                                  for d in datasets], key=lambda ab: -len(ab[-1])))

    def pathids(self, ext=None):
        base = self.project_path.parent
        rows = [['path', 'id']] + sorted([c.relative_to(base), c.cache.id]#, c.cache.uri_api, c.cache.uri_human]
                                         # slower to include the uris
                                         for c in chain((self.cwd,), self.cwd.rchildren)
        )
        return self._print_table(rows, title='Path identifiers', ext=ext)

    def terms(self, ext=None):
        # anatomy
        # cells
        # subcelluar
        import rdflib
        # FIXME cache these results and only recompute if latest changes?
        if self.options.raw:
            graph = self.summary.triples_exporter.graph
        else:
            graph = rdflib.Graph()
            self.latest_export_ttl_populate(graph)

        objects = set()
        skipped_prefixes = set()
        for t in graph:
            for e in t:
                if isinstance(e, rdflib.URIRef):
                    oid = OntId(e)
                    if oid.prefix in want_prefixes:
                        objects.add(oid)
                    else:
                        skipped_prefixes.add(oid.prefix)

        if self.options.server and isinstance(ext, types.FunctionType):
            def reformat(ot):
                return [ot.label if hasattr(ot, 'label') and ot.label else '', ot.atag(curie=True)]

        else:
            def reformat(ot):
                return [ot.label if hasattr(ot, 'label') and ot.label else '', ot.curie]

        log.info(' '.join(sorted(skipped_prefixes)))
        objs = [OntTerm(o) if o.prefix not in ('TEMP', 'sparc') or
                o.prefix == 'TEMP' and o.suffix.isdigit() else
                o for o in objects]
        term_sets = {title:[o for o in objs if o.prefix == prefix]
                     for prefix, title in
                     (('NCBITaxon', 'Species'),
                      ('UBERON', 'Anatomy and age category'),  # FIXME
                      ('FMA', 'Anatomy (FMA)'),
                      ('PATO', 'Qualities'),
                      ('tech', 'Techniques'),
                      ('unit', 'Units'),
                      ('sparc', 'MIS terms'),
                      ('TEMP', 'Suggested terms'),
                     )}

        term_sets['Other'] = set(objs) - set(ot for v in term_sets.values() for ot in v)

        for title, terms in term_sets.items():
            header = [['Label', 'CURIE']]
            rows = header + [reformat(ot) for ot in
                            sorted(terms,
                                   key=lambda ot: (ot.prefix, ot.label.lower()
                                                   if hasattr(ot, 'label') and ot.label else ''))]

            yield self._print_table(rows, title=title, ext=ext)


class Shell(Dispatcher):
    # property ports
    paths = Main.paths
    _paths = Main._paths
    _build_paths = Main._build_paths
    datasets = Main.datasets
    datasets_local = Main.datasets_local
    export_base = Main.export_base
    LATEST = Main.LATEST
    latest_export = Main.latest_export
    latest_export_ttl_populate = Main.latest_export_ttl_populate
    stash = Main.stash

    def default(self):
        from sparcur.core import AutoId, AutoInst
        datasets = list(self.datasets)
        datasets_local = list(self.datasets_local)
        dsd = {d.meta.id:d for d in datasets}
        ds = datasets
        summary = self.summary
        org = Integrator(self.project_path)

        p, *rest = self._paths
        if p.cache.is_dataset():
            intr = Integrator(p)
            j = JT(intr.data)
            #triples = list(f.triples)

        try:
            latest_datasets = self.latest_export['datasets']
        except:
            pass

        rcs = list(datasets[-1].rchildren)
        asdf = rcs[-1]
        urg = list(asdf.data)
        resp = asdf.data_headers
        embed()

    def affil(self):
        from pyontutils.utils import Async, deferred
        from sparcur.sheets import Affiliations
        a = Affiliations()
        m = a.mapping
        rors = sorted(set(_ for _ in m.values() if _))
        #dat = Async(rate=5)(deferred(lambda r:r.data)(i) for i in rors)
        dat = [r.data for r in rors]  # once the cache has been populated
        embed()

    def protocols(self):
        """ test protocol identifier functionality """
        org = Integrator(self.project_path)
        from sparcur.core import get_right_id, AutoId, DoiId, PioId, PioInst
        from pyontutils.utils import Async, deferred
        skip = '"none"', 'NA', 'no protocols', 'take protocol from other spreadsheet, '
        asdf = [us for us in sorted(org.organs_sheet.byCol.protocol_url_1)
                if us not in skip and us and ',' not in us]
        wat = [AutoId(_) for _ in asdf]
        inst = [i.asInstrumented() for i in wat]
        res = Async(rate=5)(deferred(i.resolve)(AutoId) for i in inst)
        pis = [i.asInstrumented() for i in res if isinstance(i, PioId)]
        #dat = Async(rate=5)(deferred(lambda p: p.data)(i) for i in pis)
        #dois = [d['protocol']['doi'] for d in dat if d]
        dois = [p.doi for p in pis]
        embed()

    def integration(self):
        from protcur.analysis import protc, Hybrid
        from sparcur import sheets
        #from sparcur.sheets import Organs, Progress, Grants, ISAN, Participants, Protocols as ProtocolsSheet
        from sparcur.protocols import ProtocolData, ProtcurData
        p, *rest = self._paths
        intr = Integrator(p)
        j = JT(intr.data)
        pj = list(intr.protocol_jsons)
        pc = list(intr.triples_exporter.protcur)
        #apj = [pj for c in intr.anchor.children for pj in c.protocol_jsons]
        embed()


class Fix(Shell):

    def default(self):
        pass

    def mismatch(self):
        """ once upon a time it was (still at the time of writing) possible
            to update meta on an existing file without preserving the old data
            AND without downloading the new data (insanity) this can be used to fix
            those cases, preserving the old version """
        oops = list(self.parent.status())
        [print(lmeta.as_pretty_diff(cmeta, pathobject=path, human=self.options.human))
         for path, lmeta, cmeta in oops]
        paths = [p for p, *_ in oops]

        def sf(cmeta):
            nmeta = PathMeta(id=cmeta.old_id)
            assert nmeta.id, f'No old_id for {pathmeta}'
            return nmeta

        nall = self.stash(paths, stashmetafunc=sf)
        [print(n.cache.meta.as_pretty(n)) for n in nall]
        embed()
        # once everything is in order and backed up 
        # [p.cache.fetch() for p in paths]

    def duplicates(self):
        all_ = defaultdict(list)
        if not self.options.path:
            paths = self.anchor.local.rchildren
        else:
            paths = self.paths

        for rc in paths:
            if rc.cache is None:
                if not rc.skip_cache:
                    log.critical(f'WHAT THE WHAT {rc}')

                continue

            all_[rc.cache.id].append(rc)

        def mkey(p):
            mns = p.cache.meta
            return (not bool(mns),
                    not bool(mns.updated),
                    -mns.updated.timestamp())

        dupes = {i:sorted(l, key=mkey)#, reverse=True)
                 for i, l in all_.items() if len(l) > 1}
        dv = list(dupes.values())
        deduped = [a.dedupe(b, pretend=True) for a, b, *c in dv
                   if (not log.warning(c) if c else not c)
        ]  # FIXME assumes a single dupe...
        to_remove = [d for paths, new in zip(dv, deduped) for d in paths if d != new]
        to_remove_size = [p for p in to_remove if p.cache.meta.size is not None]
        #[p.unlink() for p in to_remove if p.cache.meta.size is None] 
        breakpoint()


def main():
    from docopt import docopt, parse_defaults
    args = docopt(__doc__, version='spc 0.0.0')
    defaults = {o.name:o.value if o.argcount else None for o in parse_defaults(__doc__)}

    # set logging to file before anything else is done
    # FIXME remove this hardcoded nonsense
    from pyontutils.utils import isoformat_safe, utcnowtz
    sparc_export = Path('~/files/blackfynn_local/export').expanduser()  # no s for maximum confusion
    ll = Path(args['--log-location'].replace('${SPARC_EXPORTS}', sparc_export.as_posix()))
    if not ll.exists():
        ll.mkdir(parents=True)  # FIXME switch to a .local folder or something

    lf = ll / isoformat_safe(utcnowtz())
    lfh = logging.FileHandler(lf.as_posix())
    lfh.setFormatter(log.handlers[0].formatter)
    log.addHandler(lfh)
    logd.addHandler(lfh)

    options = Options(args, defaults)
    main = Main(options)
    if main.options.debug:
        print(main.options)

    main()


if __name__ == '__main__':
    main()
