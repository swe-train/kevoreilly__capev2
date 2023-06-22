#!/usr/bin/env python
# Copyright (C) 2010-2015 Cuckoo Foundation.
# This file is part of Cuckoo Sandbox - http://www.cuckoosandbox.org
# See the file 'docs/LICENSE' for copying permission.
import argparse
import gc
import json
import logging
import os
import platform
import resource
import signal
import sys
import time
from contextlib import suppress

try:
    from setproctitle import getproctitle, setproctitle
except ImportError:
    sys.exit("Missed dependency. Run: poetry install")

if sys.version_info[:2] < (3, 8):
    sys.exit("You are running an incompatible version of Python, please use >= 3.8")

try:
    import pebble
except ImportError:
    sys.exit("Missed dependency. Run: poetry install")

log = logging.getLogger()

sys.path.append(os.path.join(os.path.abspath(os.path.dirname(__file__)), ".."))
from concurrent.futures import TimeoutError

from lib.cuckoo.common.colors import red
from lib.cuckoo.common.config import Config
from lib.cuckoo.common.constants import CUCKOO_ROOT
from lib.cuckoo.common.path_utils import path_delete, path_exists, path_mkdir
from lib.cuckoo.common.utils import free_space_monitor
from lib.cuckoo.core.database import TASK_COMPLETED, TASK_FAILED_PROCESSING, TASK_REPORTED, Database, Task
from lib.cuckoo.core.plugins import RunProcessing, RunReporting, RunSignatures
from lib.cuckoo.core.startup import ConsoleHandler, check_linux_dist, init_modules, init_yara

cfg = Config()
logconf = Config("logging")
repconf = Config("reporting")
db = Database()

if repconf.mongodb.enabled:
    from bson.objectid import ObjectId

    from dev_utils.mongodb import mongo_find, mongo_find_one

if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
    from elasticsearch.exceptions import RequestError as ESRequestError

    from dev_utils.elasticsearchdb import elastic_handler, get_analysis_index, get_query_by_info_id

    es = elastic_handler

check_linux_dist()

pending_future_map = {}
pending_task_id_map = {}
original_proctitle = getproctitle()

# https://stackoverflow.com/questions/41105733/limit-ram-usage-to-python-program
def memory_limit(percentage: float = 0.8):
    if platform.system() != "Linux":
        print("Only works on linux!")
        return
    _, hard = resource.getrlimit(resource.RLIMIT_AS)
    resource.setrlimit(resource.RLIMIT_AS, (int(get_memory() * 1024 * percentage), hard))


def get_memory():
    with open("/proc/meminfo", "r") as mem:
        free_memory = 0
        for i in mem:
            sline = i.split()
            if str(sline[0]) == "MemAvailable:":
                free_memory = int(sline[1])
                break
    return free_memory


def process(
    target=None,
    sample_sha256=None,
    task=None,
    report=False,
    auto=False,
    capeproc=False,
    memory_debugging=False,
    debug: bool = False,
):
    # This is the results container. It's what will be used by all the
    # reporting modules to make it consumable by humans and machines.
    # It will contain all the results generated by every processing
    # module available. Its structure can be observed through the JSON
    # dump in the analysis' reports folder. (If jsondump is enabled.)

    task_dict = task.to_dict() or {}
    task_id = task_dict.get("id") or 0

    # ToDo new logger here
    handlers = init_logging(tid=str(task_id), debug=debug)
    set_formatter_fmt(task_id)
    setproctitle(f"{original_proctitle} [Task {task_id}]")
    results = {"statistics": {"processing": [], "signatures": [], "reporting": []}}
    if memory_debugging:
        gc.collect()
        log.info("(1) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))
    if memory_debugging:
        gc.collect()
        log.info("(2) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))
    RunProcessing(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("(3) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))

    RunSignatures(task=task_dict, results=results).run()
    if memory_debugging:
        gc.collect()
        log.info("(4) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))

    if report:
        if auto or capeproc:
            reprocess = False
        else:
            reprocess = report

        RunReporting(task=task.to_dict(), results=results, reprocess=reprocess).run()
        Database().set_status(task_id, TASK_REPORTED)

        if auto:
            # Is ok to delete original file, but we need to lookup on delete_bin_copy if no more pendings tasks
            if cfg.cuckoo.delete_original and target and path_exists(target):
                path_delete(target)

            if cfg.cuckoo.delete_bin_copy:
                copy_path = os.path.join(CUCKOO_ROOT, "storage", "binaries", sample_sha256)
                if path_exists(copy_path) and not db.sample_still_used(sample_sha256, task_id):
                    path_delete(copy_path)

    if memory_debugging:
        gc.collect()
        log.info("(5) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))
        for i, obj in enumerate(gc.garbage):
            log.info("(garbage) GC object #%d: type=%s", i, type(obj).__name__)

    for handler in handlers:
        if not handler:
            continue
        log.removeHandler(handler)


def init_worker():
    signal.signal(signal.SIGINT, signal.SIG_IGN)


def get_formatter_fmt(task_id=None):
    task_info = f"[Task {task_id}] " if task_id is not None else ""
    return f"%(asctime)s {task_info}[%(name)s] %(levelname)s: %(message)s"


FORMATTER = logging.Formatter(get_formatter_fmt())


def set_formatter_fmt(task_id=None):
    FORMATTER._style._fmt = get_formatter_fmt(task_id)


def init_logging(auto=False, tid=0, debug=False):
    ch = ConsoleHandler()
    ch.setFormatter(FORMATTER)
    log.addHandler(ch)

    slh = False

    if logconf.logger.syslog_process:
        slh = logging.handlers.SysLogHandler(address=logconf.logger.syslog_dev)
        slh.setFormatter(FORMATTER)
        log.addHandler(slh)

    try:
        if not path_exists(os.path.join(CUCKOO_ROOT, "log")):
            path_mkdir(os.path.join(CUCKOO_ROOT, "log"))
        if auto:
            if logconf.log_rotation.enabled:
                days = logconf.log_rotation.backup_count or 7
                fh = logging.handlers.TimedRotatingFileHandler(
                    os.path.join(CUCKOO_ROOT, "log", "process.log"), when="midnight", backupCount=int(days)
                )
            else:
                fh = logging.handlers.WatchedFileHandler(os.path.join(CUCKOO_ROOT, "log", "process.log"))
        else:
            if logconf.logger.process_analysis_folder:
                path = os.path.join(CUCKOO_ROOT, "storage", "analyses", str(tid), "process.log")
            else:
                path = os.path.join(CUCKOO_ROOT, "log", "process-%s.log" % str(tid))

            # We need to delete old log, otherwise it will append to existing one
            if path_exists(path):
                path_delete(path)

            fh = logging.handlers.WatchedFileHandler(path)

    except PermissionError:
        sys.exit("Probably executed with wrong user, PermissionError to create/access log")

    fh.setFormatter(FORMATTER)
    log.addHandler(fh)

    if debug:
        log.setLevel(logging.DEBUG)
    else:
        log.setLevel(logging.INFO)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return ch, fh, slh


def processing_finished(future):
    task_id = pending_future_map.get(future)
    try:
        _ = future.result()
        log.info("Reports generation completed")
    except TimeoutError as error:
        log.error("Processing Timeout %s. Function: %s", error, error.args[1])
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except pebble.ProcessExpired as error:
        log.error("Exception when processing task: %s", error, exc_info=True)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)
    except Exception as error:
        log.error("Exception when processing task: %s", error, exc_info=True)
        Database().set_status(task_id, TASK_FAILED_PROCESSING)

    pending_future_map.pop(future)
    pending_task_id_map.pop(task_id)
    set_formatter_fmt()
    setproctitle(original_proctitle)


def autoprocess(
    parallel=1, failed_processing=False, maxtasksperchild=7, memory_debugging=False, processing_timeout=300, debug: bool = False
):
    maxcount = cfg.cuckoo.max_analysis_count
    count = 0
    # pool = multiprocessing.Pool(parallel, init_worker)
    pool = False
    try:
        memory_limit()
        log.info("Processing analysis data")
        with pebble.ProcessPool(max_workers=parallel, max_tasks=maxtasksperchild, initializer=init_worker) as pool:
            # CAUTION - big ugly loop ahead.
            while count < maxcount or not maxcount:

                # If not enough free disk space is available, then we print an
                # error message and wait another round (this check is ignored
                # when the freespace configuration variable is set to zero).
                if cfg.cuckoo.freespace:
                    # Resolve the full base path to the analysis folder, just in
                    # case somebody decides to make a symbolic link out of it.
                    dir_path = os.path.join(CUCKOO_ROOT, "storage", "analyses")
                    free_space_monitor(dir_path, processing=True)

                # If still full, don't add more (necessary despite pool).
                if len(pending_task_id_map) >= parallel:
                    time.sleep(5)
                    continue
                if failed_processing:
                    tasks = db.list_tasks(status=TASK_FAILED_PROCESSING, limit=parallel, order_by=Task.completed_on.asc())
                else:
                    tasks = db.list_tasks(status=TASK_COMPLETED, limit=parallel, order_by=Task.completed_on.asc())
                added = False
                # For loop to add only one, nice. (reason is that we shouldn't overshoot maxcount)
                for task in tasks:
                    # Not-so-efficient lock.
                    if pending_task_id_map.get(task.id):
                        continue

                    log.info("Processing analysis data for Task #%d", task.id)
                    sample_hash = ""
                    if task.category != "url":
                        sample = db.view_sample(task.sample_id)
                        if sample:
                            sample_hash = sample.sha256

                    args = task.target, sample_hash
                    kwargs = dict(report=True, auto=True, task=task, memory_debugging=memory_debugging, debug=debug)
                    if memory_debugging:
                        gc.collect()
                        log.info("(before) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))
                    # result = pool.apply_async(process, args, kwargs)
                    future = pool.schedule(process, args, kwargs, timeout=processing_timeout)
                    pending_future_map[future] = task.id
                    pending_task_id_map[task.id] = future
                    future.add_done_callback(processing_finished)
                    if memory_debugging:
                        gc.collect()
                        log.info("(after) GC object counts: %d, %d", len(gc.get_objects()), len(gc.garbage))
                    count += 1
                    added = True
                    break

                if not added:
                    # don't hog cpu
                    time.sleep(5)
    except KeyboardInterrupt:
        # ToDo verify in finally
        # pool.terminate()
        raise
    except MemoryError:
        mem = get_memory() / 1024 / 1024
        print("Remain: %.2f GB" % mem)
        sys.stderr.write("\n\nERROR: Memory Exception\n")
        sys.exit(1)
    except Exception:
        import traceback

        traceback.print_exc()
    finally:
        if pool:
            pool.close()
            pool.join()


def _load_report(task_id: int, return_one: bool = False):

    if repconf.mongodb.enabled:
        if return_one:
            analysis = mongo_find_one("analysis", {"info.id": task_id}, sort=[("_id", -1)])
            for process in analysis.get("behavior", {}).get("processes", []):
                calls = [ObjectId(call) for call in process["calls"]]
                process["calls"] = []
                for call in mongo_find("calls", {"_id": {"$in": calls}}, sort=[("_id", 1)]) or []:
                    process["calls"] += call["calls"]
            return analysis

        else:
            return mongo_find("analysis", {"info.id": task_id})

    if repconf.elasticsearchdb.enabled and not repconf.elasticsearchdb.searchonly:
        try:
            analyses = (
                es.search(index=get_analysis_index(), query=get_query_by_info_id(task_id), sort={"info.id": {"order": "desc"}})
                .get("hits", {})
                .get("hits", [])
            )
            if analyses:
                if return_one:
                    return analyses[0]
                else:
                    return analyses
        except ESRequestError as e:
            print(e)

    return False


def parse_id(id_string: str):
    if id_string == "auto":
        return id_string
    id_string = id_string.replace(" ", "")
    blocks = [block.split("-") for block in id_string.split(",")]
    for index, block in enumerate(blocks):
        block = list(map(int, block))
        if len(block) == 2:
            if block[1] < block[0]:
                raise TypeError("Invalid id input")
            else:
                blocks[index] = block
        elif len(block) == 1:
            blocks[index] = (block[0], block[0])
        else:
            raise TypeError("Invalid id input")
    return blocks


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "id",
        type=parse_id,
        help="ID of the analysis to process (auto for continuous processing of unprocessed tasks). Can be 1 or 1-10",
    )
    parser.add_argument("-c", "--caperesubmit", help="Allow CAPE resubmit processing.", action="store_true", required=False)
    parser.add_argument("-d", "--debug", help="Display debug messages", action="store_true", required=False)
    parser.add_argument("-r", "--report", help="Re-generate report", action="store_true", required=False)
    parser.add_argument(
        "-p", "--parallel", help="Number of parallel threads to use (auto mode only).", type=int, required=False, default=1
    )
    parser.add_argument(
        "-fp", "--failed-processing", help="reprocess failed processing", action="store_true", required=False, default=False
    )
    parser.add_argument(
        "-mc", "--maxtasksperchild", help="Max children tasks per worker", action="store", type=int, required=False, default=7
    )
    parser.add_argument(
        "-md",
        "--memory-debugging",
        help="Enable logging garbage collection related info",
        action="store_true",
        required=False,
        default=False,
    )
    parser.add_argument(
        "-pt",
        "--processing-timeout",
        help="Max amount of time spent in processing before we fail a task",
        action="store",
        type=int,
        required=False,
        default=300,
    )
    testing_args = parser.add_argument_group("Signature testing options")
    testing_args.add_argument(
        "-sig",
        "--signatures",
        help="Re-execute signatures on the report, doesn't work for signature with self.get_raw_argument, use self.get_argument",
        action="store_true",
        default=False,
        required=False,
    )
    testing_args.add_argument(
        "-sn",
        "--signature-name",
        help="Run only one signature. To be used with --signature. Example -sig -sn cape_detected_threat",
        action="store",
        default=False,
        required=False,
    )
    testing_args.add_argument(
        "-jr",
        "--json-report",
        help="Path to json report, only if data not in mongo/default report location",
        action="store",
        default=False,
        required=False,
    )
    args = parser.parse_args()

    init_yara()
    init_modules()
    if args.id == "auto":
        if not logconf.logger.process_per_task_log:
            init_logging(auto=True, debug=args.debug)
        autoprocess(
            parallel=args.parallel,
            failed_processing=args.failed_processing,
            maxtasksperchild=args.maxtasksperchild,
            memory_debugging=args.memory_debugging,
            processing_timeout=args.processing_timeout,
            debug=args.debug,
        )
    else:
        for start, end in args.id:
            for num in range(start, end + 1):
                set_formatter_fmt(num)
                log.debug("Processing task")
                if not path_exists(os.path.join(CUCKOO_ROOT, "storage", "analyses", str(num))):
                    sys.exit(red("\n[-] Analysis folder doesn't exist anymore\n"))
                # handlers = init_logging(tid=str(num), debug=args.debug)
                task = Database().view_task(num)
                # Add sample lookup as we point to sample from TMP. Case when delete_original=on
                if not path_exists(task.target):
                    samples = Database().sample_path_by_hash(task_id=task.id)
                    for sample in samples:
                        if path_exists(sample):
                            task.__setattr__("target", sample)
                            break

                if args.signatures:
                    report = False
                    results = _load_report(num, return_one=True)
                    if not results:
                        # fallback to json
                        report = os.path.join(CUCKOO_ROOT, "storage", "analyses", str(num), "reports", "report.json")
                        if not path_exists(report):
                            if args.json_report and path_exists(args.json_report):
                                report = args.json_report
                            else:
                                sys.exit(f"File {report} doest exist")
                        if report:
                            results = json.load(open(report))
                    if results is not None:
                        # If the "statistics" key-value pair has not been set by now, set it here
                        if "statistics" not in results:
                            results["statistics"] = {"signatures": []}
                        RunSignatures(task=task.to_dict(), results=results).run(args.signature_name)
                else:
                    process(
                        task=task,
                        report=args.report,
                        capeproc=args.caperesubmit,
                        memory_debugging=args.memory_debugging,
                        debug=args.debug,
                    )
                log.debug("Finished processing task")
                set_formatter_fmt()


if __name__ == "__main__":
    with suppress(KeyboardInterrupt):
        main()
