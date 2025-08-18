import os

os.environ["TOKENIZERS_PARALLELISM"] = "false"
import argparse
import os
import time
from pathlib import Path
from typing import List, Dict, Any, Tuple
import yaml
import pandas as pd
import numpy as np
from dotenv import load_dotenv
from tqdm import tqdm
import matplotlib.pyplot as plt

from utils.metrics import hits_at_k
from databases.qdrant_client import Qdrant
from databases.milvus_client import Milvus
from databases.weaviate_client import WeaviateDB


def load_embeddings(parquet_path: str):
    df = pd.read_parquet(parquet_path)
    vectors = np.array(df["embedding"].tolist(), dtype=np.float32)
    payloads = df[["track", "artist", "genre", "seeds", "text"]].to_dict(
        orient="records"
    )
    return vectors, payloads


def embed_query(q: str, model) -> List[float]:
    v = model.encode([q], normalize_embeddings=True)[0]
    return v.tolist()


def get_db(name: str, args) -> Any:
    name = name.lower()
    if name == "qdrant":
        return Qdrant(url=os.getenv("QDRANT_URL", "http://localhost:6333"))
    if name == "milvus":
        return Milvus(
            host=os.getenv("MILVUS_HOST", "localhost"),
            port=os.getenv("MILVUS_PORT", "19530"),
        )
    if name == "weaviate":
        return WeaviateDB(url=os.getenv("WEAVIATE_URL", "http://localhost:8080"))
    if name == "pinecone":
        from databases.pinecone_client import PineconeClient

        return PineconeClient()
    if name == "topk":
        from databases.topk_client import TopKClient

        return TopKClient()


def main():
    load_dotenv()
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", required=True, help="Original CSV path")
    ap.add_argument("--embeddings", required=True, help="Parquet with embeddings")
    ap.add_argument(
        "--dbs", nargs="+", default=["qdrant"], help="Which DBs to benchmark"
    )
    ap.add_argument("--queries", default="queries.yaml", help="YAML file with queries")
    ap.add_argument("--topk", type=int, default=10)
    ap.add_argument(
        "--topk_sweep",
        nargs="*",
        type=int,
        default=None,
        help="List of k values to sweep (e.g. 5 10 50)",
    )
    ap.add_argument(
        "--concurrency", type=int, default=1, help="Number of concurrent query workers"
    )
    ap.add_argument("--repetitions", type=int, default=3)
    ap.add_argument(
        "--warmup", type=int, default=1, help="Warm-up passes per DB (not timed)"
    )
    ap.add_argument("--query_model", default="sentence-transformers/all-MiniLM-L6-v2")
    args = ap.parse_args()

    vectors, payloads = load_embeddings(args.embeddings)
    dim = vectors.shape[1]
    # Check normalization: all vectors should have norm ~1
    norms = np.linalg.norm(vectors, axis=1)
    if not np.allclose(norms, 1.0, atol=1e-3):
        print(
            "Warning: Not all vectors are normalized! Min norm:",
            norms.min(),
            "Max norm:",
            norms.max(),
        )
    else:
        print("All vectors are normalized (L2 norm ~1)")

    with open(args.queries, "r") as f:
        cfg = yaml.safe_load(f)
    queries = cfg["queries"]

    # Preload the query embedding model once to avoid repeated loads
    from sentence_transformers import SentenceTransformer

    query_model = SentenceTransformer(args.query_model)

    # Exact baseline recall helper (cosine on normalized vectors)
    def exact_topk_indices(qv: np.ndarray, mat: np.ndarray, k: int) -> np.ndarray:
        # qv: shape (D,), mat: shape (N, D)
        sims = mat @ qv  # dot product = cosine since normalized
        if k >= len(sims):
            return np.argsort(-sims)
        idx = np.argpartition(-sims, k)[:k]
        # sort top-k for stable order
        return idx[np.argsort(-sims[idx])]

    results = {}
    # Add config metadata
    results["_config"] = {
        "batch_size": 2000,
        "hnsw_params": {"M": 16, "efConstruction": 128, "ef": 128},
        "metric": "COSINE",
        "model": args.query_model,
        "dataset_size": len(vectors),
        "repetitions": args.repetitions,
    }

    topks = args.topk_sweep or [args.topk]
    for db_name in args.dbs:
        print(f"Setting up {db_name}")
        db = get_db(db_name, args)
        t0 = time.time()
        if db_name.lower() == "topk":
            db.setup()
        else:
            db.setup(dim=dim)
        t1 = time.time()
        if db_name.lower() == "pinecone":
            db.upsert(vectors=vectors.tolist(), payloads=payloads, batch_size=200)
        else:
            db.upsert(vectors=vectors.tolist(), payloads=payloads)
        ingest_time = time.time() - t1
        setup_time = t1 - t0

        # Optional warm-up passes (not timed)
        for _ in range(max(0, args.warmup)):
            for q in cfg["queries"]:
                q_vec = embed_query(q["text"], query_model)
                _ = db.search(q_vec, top_k=topks[0])

        results[db_name] = {}
        for k in topks:
            latencies = []
            hits = []
            recalls = []
            qps_by_rep = []
            first_latency = None
            import random

            # Use the same concurrency for all DBs
            db_concurrency = args.concurrency

            for rep in range(args.repetitions):
                order = list(range(len(queries)))
                random.shuffle(order)
                from concurrent.futures import ThreadPoolExecutor, as_completed

                def _one_query(q):
                    q_vec = embed_query(q["text"], query_model)
                    s0 = time.time()
                    res = db.search(q_vec, top_k=k)
                    latency = time.time() - s0
                    return latency, res, q

                s_all = time.time()
                futs = []
                with ThreadPoolExecutor(max_workers=db_concurrency) as ex:
                    futs = [ex.submit(_one_query, queries[idx]) for idx in order]
                    for f in as_completed(futs):
                        latency, res, q = f.result()
                        if first_latency is None:
                            first_latency = latency
                        latencies.append(latency)
                        res_payloads = [r["payload"] for r in res]
                        hitk = hits_at_k(res_payloads, q["expected"])
                        hits.append(hitk)
                        # Compute exact recall@k using baseline
                        q_vec = embed_query(q["text"], query_model)
                        true_idx = exact_topk_indices(
                            np.array(q_vec, dtype=np.float32), vectors, k
                        )
                        if db_name.lower() == "pinecone":
                            # Compare as strings for Pinecone
                            true_set = set(str(i) for i in true_idx.tolist())
                            res_ids = []
                            for r in res:
                                pid = r.get("payload", {}).get("row_id")
                                if pid is not None:
                                    res_ids.append(str(pid))
                                else:
                                    rid = r.get("id")
                                    if rid is not None:
                                        res_ids.append(str(rid))
                            inter = len(true_set.intersection(set(res_ids)))
                        else:
                            true_set = set(int(i) for i in true_idx.tolist())
                            res_ids = []
                            for r in res:
                                pid = r.get("payload", {}).get("row_id")
                                if isinstance(pid, (int, np.integer)):
                                    res_ids.append(int(pid))
                                else:
                                    rid = r.get("id")
                                    if isinstance(rid, (int, np.integer)):
                                        res_ids.append(int(rid))
                            inter = len(true_set.intersection(set(res_ids)))
                        recalls.append(inter / float(k) if k > 0 else 0.0)
                wall = time.time() - s_all
                qps = len(queries) / wall if wall > 0 else 0.0
                qps_by_rep.append(qps)

            avg_latency = float(np.mean(latencies)) if latencies else None
            p50_latency = float(np.percentile(latencies, 50)) if latencies else None
            p95_latency = float(np.percentile(latencies, 95)) if latencies else None
            p99_latency = float(np.percentile(latencies, 99)) if latencies else None
            jitter = float(np.std(latencies)) if latencies else None
            avg_hitk = float(np.mean(hits)) if hits else None
            avg_recall = float(np.mean(recalls)) if recalls else None
            avg_qps = float(np.mean(qps_by_rep)) if qps_by_rep else None

            results[db_name][f"k={k}"] = {
                "setup_time_sec": setup_time,
                "ingest_time_sec": ingest_time,
                "avg_query_latency_sec": avg_latency,
                "p50_query_latency_sec": p50_latency,
                "p95_query_latency_sec": p95_latency,
                "p99_query_latency_sec": p99_latency,
                "latency_stddev_sec": jitter,
                "first_query_latency_sec": first_latency,
                f"avg_hits_at_{k}": avg_hitk,
                f"avg_recall_at_{k}": avg_recall,
                "avg_qps": avg_qps,
            }

        # Do not teardown Pinecone after benchmarking so the index persists for the search engine
        if db_name.lower() != "pinecone":
            db.teardown()

    # Save results
    out_dir = Path("results")
    out_dir.mkdir(exist_ok=True)

    with open(out_dir / "metrics.json", "w") as f:
        import json

        json.dump(results, f, indent=2)
    print("Saved metrics.json")

    # --- Call plot_benchmarks.py to generate summary image ---
    import subprocess

    plot_script = Path(__file__).parent / "plot_benchmarks.py"
    metrics_path = out_dir / "metrics.json"
    out_prefix = out_dir / "benchmark_summary"
    try:
        subprocess.run(
            ["python", str(plot_script), str(metrics_path), str(out_prefix)], check=True
        )
    except Exception as e:
        print(f"[WARN] Could not run plot_benchmarks.py: {e}")

    # --- New plotting for per-k, per-metric results ---
    # Get all k values
    all_ks = set()
    for db_name in results:
        if db_name == "_config":
            continue
        all_ks.update(
            [
                int(k.split("=")[1])
                for k in results[db_name].keys()
                if k.startswith("k=")
            ]
        )
    all_ks = sorted(all_ks)
    # List of metrics to plot
    metric_keys = [
        ("avg_query_latency_sec", "Avg Query Latency (sec)"),
        ("p50_query_latency_sec", "P50 Latency (sec)"),
        ("p95_query_latency_sec", "P95 Latency (sec)"),
        ("p99_query_latency_sec", "P99 Latency (sec)"),
        ("latency_stddev_sec", "Latency Stddev (sec)"),
        ("first_query_latency_sec", "First Query Latency (sec)"),
        ("avg_qps", "Avg QPS"),
        ("ingest_time_sec", "Ingest Time (sec)"),
        ("setup_time_sec", "Setup Time (sec)"),
    ]
    # Add hits/recall for each k
    for k in all_ks:
        metric_keys.append((f"avg_hits_at_{k}", f"Avg Hits in Top {k}"))
        metric_keys.append((f"avg_recall_at_{k}", f"Avg Recall@{k}"))

    # For each k, plot each metric for all DBs

    for k in all_ks:
        k_dir = out_dir / f"k{k}"
        k_dir.mkdir(exist_ok=True)
        for metric_key, metric_label in metric_keys:
            values = []
            db_labels = []
            for db_name in results:
                if db_name == "_config":
                    continue
                kkey = f"k={k}"
                v = results[db_name].get(kkey, {}).get(metric_key, None)
                if v is not None:
                    values.append(v)
                    db_labels.append(db_name)
            if not values:
                continue
            fig, ax = plt.subplots(figsize=(max(6, len(db_labels) * 1.5), 5))
            bars = ax.bar(db_labels, values, color="skyblue")
            ax.set_title(f"{metric_label} (k={k})")
            ax.set_ylabel(metric_label)
            for bar, value in zip(bars, values):
                ax.annotate(
                    f"{value:.4f}",
                    xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 5),
                    textcoords="offset points",
                    ha="center",
                    va="bottom",
                    fontsize=11,
                    fontweight="bold",
                )
            plt.tight_layout()
            fname = (
                metric_label.lower()
                .replace(" ", "_")
                .replace("@", "at")
                .replace("(", "")
                .replace(")", "")
                .replace("/", "_")
                + f"_k{k}.png"
            )
            plt.savefig(k_dir / fname, bbox_inches="tight")
            plt.close(fig)
            print(f"Saved {fname} in {k_dir}/")

        # Table for all metrics for this k
        table_metrics = [
            "avg_query_latency_sec",
            "p50_query_latency_sec",
            "p95_query_latency_sec",
            "p99_query_latency_sec",
            "latency_stddev_sec",
            "first_query_latency_sec",
            "avg_qps",
            "ingest_time_sec",
            "setup_time_sec",
            f"avg_hits_at_{k}",
            f"avg_recall_at_{k}",
        ]
        db_labels = [db for db in results if db != "_config"]
        cell_text = []
        row_labels = []
        for metric_key in table_metrics:
            row = []
            for db_name in db_labels:
                v = results[db_name].get(f"k={k}", {}).get(metric_key, None)
                row.append(
                    f"{v:.4f}"
                    if isinstance(v, float)
                    else (str(v) if v is not None else "-")
                )
            cell_text.append(row)
            row_labels.append(metric_key)
        fig, ax = plt.subplots(
            figsize=(max(6, len(db_labels) * 2), 2 + len(table_metrics))
        )
        table = ax.table(
            cellText=cell_text,
            rowLabels=row_labels,
            colLabels=db_labels,
            loc="center",
            cellLoc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(12)
        table.scale(1.2, 1.5)
        ax.axis("off")
        plt.title(f"Benchmark Metrics Table (k={k})", fontsize=16, pad=20)
        plt.tight_layout()
        plt.savefig(k_dir / f"metrics_table_k{k}.png", bbox_inches="tight")
        plt.close(fig)
        print(f"Saved metrics_table_k{k}.png in {k_dir}/")


if __name__ == "__main__":
    main()
