import contextlib
import signal
import sys
import threading
import types

import pandas as pd
import tqdm
from tqdm.contrib.logging import logging_redirect_tqdm

import og_dspy

try:
    from IPython.display import HTML
    from IPython.display import display as ipython_display
except ImportError:
    ipython_display = print

    def HTML(x) -> str:  # noqa: N802
        return x


from concurrent.futures import ThreadPoolExecutor, as_completed

# TODO: Counting failures and having a max_failure count. When that is exceeded (also just at the end),
# we print the number of failures, the first N examples that failed, and the first N exceptions raised.


class Evaluate:
    def __init__(
        self,
        *,
        devset,
        metric=None,
        num_threads=1,
        display_progress=False,
        display_table=False,
        max_errors=5,
        return_all_scores=False,
        return_outputs=False,
        **_kwargs,
    ):
        self.devset = devset
        self.metric = metric
        self.num_threads = num_threads
        self.display_progress = display_progress
        self.display_table = display_table
        self.max_errors = max_errors
        self.error_count = 0
        self.error_lock = threading.Lock()
        self.cancel_jobs = threading.Event()
        self.return_all_scores = return_all_scores
        self.return_outputs = return_outputs

        if "display" in _kwargs:
            og_dspy.logger.warning(
                "DeprecationWarning: 'display' has been deprecated. To see all information for debugging,"
                " use 'dspy.set_log_level('debug')'. In the future this will raise an error.",
            )

    def _execute_single_thread(self, wrapped_program, devset, display_progress):
        ncorrect = 0
        ntotal = 0
        reordered_devset = []

        pbar = tqdm.tqdm(total=len(devset), dynamic_ncols=True, disable=not display_progress, file=sys.stdout)
        for idx, arg in devset:
            with logging_redirect_tqdm():
                example_idx, example, prediction, score = wrapped_program(idx, arg)
                reordered_devset.append((example_idx, example, prediction, score))
                ncorrect += score
                ntotal += 1
                self._update_progress(pbar, ncorrect, ntotal)

        pbar.close()

        return reordered_devset, ncorrect, ntotal

    def _execute_multi_thread(self, wrapped_program, devset, num_threads, display_progress):
        ncorrect = 0
        ntotal = 0
        reordered_devset = []
        job_cancelled = "cancelled"

        # context manger to handle sigint
        @contextlib.contextmanager
        def interrupt_handler_manager():
            """Sets the cancel_jobs event when a SIGINT is received."""
            default_handler = signal.getsignal(signal.SIGINT)

            def interrupt_handler(sig, frame):
                self.cancel_jobs.set()
                og_dspy.logger.warning("Received SIGINT. Cancelling evaluation.")
                default_handler(sig, frame)

            signal.signal(signal.SIGINT, interrupt_handler)
            yield
            # reset to the default handler
            signal.signal(signal.SIGINT, default_handler)

        def cancellable_wrapped_program(idx, arg):
            # If the cancel_jobs event is set, return the cancelled_job literal
            if self.cancel_jobs.is_set():
                return None, None, job_cancelled, None
            return wrapped_program(idx, arg)

        with ThreadPoolExecutor(max_workers=num_threads) as executor, interrupt_handler_manager():
            futures = {executor.submit(cancellable_wrapped_program, idx, arg) for idx, arg in devset}
            pbar = tqdm.tqdm(total=len(devset), dynamic_ncols=True, disable=not display_progress)

            for future in as_completed(futures):
                example_idx, example, prediction, score = future.result()

                # use the cancelled_job literal to check if the job was cancelled - use "is" not "=="
                # in case the prediction is "cancelled" for some reason.
                if prediction is job_cancelled:
                    continue

                reordered_devset.append((example_idx, example, prediction, score))
                ncorrect += score
                ntotal += 1
                self._update_progress(pbar, ncorrect, ntotal)
            pbar.close()

        if self.cancel_jobs.is_set():
            og_dspy.logger.warning("Evaluation was cancelled. The results may be incomplete.")
            raise KeyboardInterrupt

        return reordered_devset, ncorrect, ntotal

    def _update_progress(self, pbar, ncorrect, ntotal):
        pbar.set_description(f"Average Metric: {ncorrect} / {ntotal}  ({round(100 * ncorrect / ntotal, 1)})")
        pbar.update()

    def __call__(
        self,
        program,
        metric=None,
        devset=None,
        num_threads=None,
        display_progress=None,
        display_table=None,
        return_all_scores=None,
        return_outputs=None,
    ):
        metric = metric if metric is not None else self.metric
        devset = devset if devset is not None else self.devset
        num_threads = num_threads if num_threads is not None else self.num_threads
        display_progress = display_progress if display_progress is not None else self.display_progress
        display_table = display_table if display_table is not None else self.display_table
        return_all_scores = return_all_scores if return_all_scores is not None else self.return_all_scores
        return_outputs = return_outputs if return_outputs is not None else self.return_outputs
        results = []

        def wrapped_program(example_idx, example):
            # NOTE: TODO: Won't work if threads create threads!
            thread_stacks = og_dspy.settings.stack_by_thread
            creating_new_thread = threading.get_ident() not in thread_stacks
            if creating_new_thread:
                thread_stacks[threading.get_ident()] = list(og_dspy.settings.main_stack)

            try:
                prediction = program(**example.inputs())
                score = metric(
                    example,
                    prediction,
                )  # FIXME: TODO: What's the right order? Maybe force name-based kwargs!

                # increment assert and suggest failures to program's attributes
                if hasattr(program, "_assert_failures"):
                    program._assert_failures += og_dspy.settings.get("assert_failures")
                if hasattr(program, "_suggest_failures"):
                    program._suggest_failures += og_dspy.settings.get("suggest_failures")

                return example_idx, example, prediction, score
            except Exception as e:
                with self.error_lock:
                    self.error_count += 1
                    current_error_count = self.error_count
                if current_error_count >= self.max_errors:
                    raise e

                og_dspy.logger.error(f"Error for example in dev set: \t\t {e}")

                return example_idx, example, {}, 0.0
            finally:
                if creating_new_thread:
                    del thread_stacks[threading.get_ident()]

        devset = list(enumerate(devset))
        tqdm.tqdm._instances.clear()

        if num_threads == 1:
            reordered_devset, ncorrect, ntotal = self._execute_single_thread(wrapped_program, devset, display_progress)
        else:
            reordered_devset, ncorrect, ntotal = self._execute_multi_thread(
                wrapped_program,
                devset,
                num_threads,
                display_progress,
            )

        og_dspy.logger.info(f"Average Metric: {ncorrect} / {ntotal} ({round(100 * ncorrect / ntotal, 1)}%)")

        predicted_devset = sorted(reordered_devset)

        if return_outputs:  # Handle the return_outputs logic
            results = [(example, prediction, score) for _, example, prediction, score in predicted_devset]

        data = [
            merge_dicts(example, prediction) | {"correct": score} for _, example, prediction, score in predicted_devset
        ]

        result_df = pd.DataFrame(data)

        # Truncate every cell in the DataFrame (DataFrame.applymap was renamed to DataFrame.map in Pandas 2.1.0)
        result_df = result_df.map(truncate_cell) if hasattr(result_df, "map") else result_df.applymap(truncate_cell)

        # Rename the 'correct' column to the name of the metric object
        metric_name = metric.__name__ if isinstance(metric, types.FunctionType) else metric.__class__.__name__
        result_df = result_df.rename(columns={"correct": metric_name})

        if display_table:
            if isinstance(display_table, bool):
                df_to_display = result_df.copy()
                truncated_rows = 0
            else:
                df_to_display = result_df.head(display_table).copy()
                truncated_rows = len(result_df) - display_table

            styled_df = configure_dataframe_display(df_to_display, metric_name)

            ipython_display(styled_df)

            if truncated_rows > 0:
                # Simplified message about the truncated rows
                message = f"""
                <div style='
                    text-align: center;
                    font-size: 16px;
                    font-weight: bold;
                    color: #555;
                    margin: 10px 0;'>
                    ... {truncated_rows} more rows not displayed ...
                </div>
                """
                ipython_display(HTML(message))

        if return_all_scores and return_outputs:
            return round(100 * ncorrect / ntotal, 2), results, [score for *_, score in predicted_devset]
        if return_all_scores:
            return round(100 * ncorrect / ntotal, 2), [score for *_, score in predicted_devset]
        if return_outputs:
            return round(100 * ncorrect / ntotal, 2), results

        return round(100 * ncorrect / ntotal, 2)


def merge_dicts(d1, d2) -> dict:
    merged = {}
    for k, v in d1.items():
        if k in d2:
            merged[f"example_{k}"] = v
        else:
            merged[k] = v

    for k, v in d2.items():
        if k in d1:
            merged[f"pred_{k}"] = v
        else:
            merged[k] = v

    return merged


def truncate_cell(content) -> str:
    """Truncate content of a cell to 25 words."""
    words = str(content).split()
    if len(words) > 25:
        return " ".join(words[:25]) + "..."
    return content


def configure_dataframe_display(df, metric_name) -> pd.DataFrame:
    """Set various pandas display options for DataFrame."""
    pd.options.display.max_colwidth = None
    pd.set_option("display.max_colwidth", 20)  # Adjust the number as needed
    pd.set_option("display.width", 400)  # Adjust

    df[metric_name] = df[metric_name].apply(lambda x: f"✔️ [{x}]" if x else str(x))

    # Return styled DataFrame
    return df.style.set_table_styles(
        [
            {"selector": "th", "props": [("text-align", "left")]},
            {"selector": "td", "props": [("text-align", "left")]},
        ],
    ).set_properties(
        **{
            "text-align": "left",
            "white-space": "pre-wrap",
            "word-wrap": "break-word",
            "max-width": "400px",
        },
    )


# FIXME: TODO: The merge_dicts stuff above is way too quick and dirty.
# TODO: the display_table can't handle False but can handle 0!
# Not sure how it works with True exactly, probably fails too.
