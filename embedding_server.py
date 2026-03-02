"""
独立 Embedding 服务 - 单进程、只加载一次模型，供主服务通过 HTTP 调用。
启动: python embedding_server.py
环境变量: EMBEDDING_MODEL, EMBEDDING_DEVICE, EMBEDDING_BATCH_SIZE, EMBEDDING_SERVER_PORT
混合检索: /embed_sparse 使用固定 BGE-M3 模型，需 pip install FlagEmbedding
"""
import os
import time

from dotenv import load_dotenv
load_dotenv()

# Sparse 向量模型（混合检索）：使用本地目录
_SPARSE_MODEL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bge-m3")
SPARSE_MODEL_NAME = _SPARSE_MODEL_DIR

# 在进程内只加载一次模型（模块级单例）
_model = None
_sparse_model = None
_batch_size = 32


def _get_model():
    global _model, _batch_size
    if _model is not None:
        return _model
    import torch  # noqa: F401
    from sentence_transformers import SentenceTransformer
    from loguru import logger

    embedding_model = os.getenv('EMBEDDING_MODEL', 'C:/Users/HX/Documents/KP/news-search/models/qwen3-embedding-0.6b')
    embedding_device = os.getenv('EMBEDDING_DEVICE', 'cuda')
    _batch_size = int(os.getenv('EMBEDDING_BATCH_SIZE', 32))

    logger.info(f"[EmbeddingServer] 正在加载模型: {embedding_model}, device={embedding_device}")
    _model = SentenceTransformer(embedding_model, device=embedding_device)
    if embedding_device == 'cuda' and torch.cuda.is_available():
        logger.info(f"[EmbeddingServer] CUDA: {torch.cuda.get_device_name(0)}")
    else:
        logger.warning("[EmbeddingServer] CUDA 不可用，使用 CPU")
    logger.info(f"[EmbeddingServer] 模型加载完成，batch_size={_batch_size}")
    return _model


def _get_sparse_model():
    """懒加载 BGE-M3 用于 sparse/lexical 向量（混合检索），从本地 models/bge-m3 加载，需先运行 scripts/download_bge_m3.py。"""
    global _sparse_model
    if _sparse_model is not None:
        return _sparse_model
    if not os.path.isdir(SPARSE_MODEL_NAME):
        from loguru import logger
        logger.warning(
            f"[EmbeddingServer] Sparse 模型目录不存在: {SPARSE_MODEL_NAME}，请先运行: python scripts/download_bge_m3.py"
        )
        return None
    try:
        from loguru import logger
        from FlagEmbedding import BGEM3FlagModel
        logger.info(f"[EmbeddingServer] 正在加载 Sparse 模型（本地）: {SPARSE_MODEL_NAME}")
        _sparse_model = BGEM3FlagModel(SPARSE_MODEL_NAME, use_fp16=True)
        logger.info("[EmbeddingServer] Sparse 模型加载完成")
        return _sparse_model
    except ImportError as e:
        from loguru import logger
        logger.warning(f"[EmbeddingServer] Sparse 模型未安装或加载失败（需 pip install FlagEmbedding）: {e}")
        return None
    except Exception as e:
        from loguru import logger
        logger.warning(f"[EmbeddingServer] Sparse 模型加载失败: {e}")
        return None


def create_app():
    from flask import Flask, request, jsonify

    app = Flask(__name__)

    @app.route('/embedding_dim', methods=['GET'])
    def embedding_dim():
        model = _get_model()
        dim = model.get_sentence_embedding_dimension()
        return jsonify({"dim": dim})

    @app.route('/embed', methods=['POST'])
    def embed():
        model = _get_model()
        body = request.get_json() or {}
        texts = body.get('texts')
        if texts is None and 'text' in body:
            texts = [body['text']]
        if not texts:
            return jsonify({"error": "missing texts or text"}), 400
        normalize_embeddings = body.get('normalize_embeddings', True)
        prompt_name = body.get('prompt_name')

        encode_kw = dict(
            batch_size=_batch_size,
            normalize_embeddings=normalize_embeddings,
            show_progress_bar=False,
        )
        if prompt_name and getattr(model, 'prompts', None) and prompt_name in model.prompts:
            encode_kw['prompt_name'] = prompt_name

        t0 = time.perf_counter()
        embeddings = model.encode(texts, **encode_kw)
        elapsed_ms = (time.perf_counter() - t0) * 1000

        if len(texts) == 1:
            out = embeddings[0].tolist()
        else:
            out = embeddings.tolist()
        return jsonify({"embeddings": out, "elapsed_ms": round(elapsed_ms, 2)})

    @app.route('/embed_sparse', methods=['POST'])
    def embed_sparse():
        """返回 BGE-M3 等模型的 sparse（lexical）向量，用于混合检索。需设置 EMBEDDING_SPARSE_MODEL。"""
        sparse_model = _get_sparse_model()
        if sparse_model is None:
            return jsonify({"error": "sparse model not available (install FlagEmbedding)"}), 501
        body = request.get_json() or {}
        texts = body.get('texts') or ([body['text']] if 'text' in body else [])
        if not texts:
            return jsonify({"error": "missing texts or text"}), 400
        t0 = time.perf_counter()
        out = sparse_model.encode(
            texts,
            return_dense=False,
            return_sparse=True,
            return_colbert_vecs=False,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000
        lexical = out.get('lexical_weights') or []
        sparse_list = []
        for i in range(len(texts)):
            if i < len(lexical):
                lw = lexical[i]
                if hasattr(lw, 'items'):
                    pairs = list(lw.items())
                    idx = [int(k) for k, _ in pairs]
                    val = [float(v) for _, v in pairs]
                else:
                    idx, val = [], []
            else:
                idx, val = [], []
            sparse_list.append({"indices": idx, "values": val})
        return jsonify({"sparse": sparse_list, "elapsed_ms": round(elapsed_ms, 2)})

    @app.route('/health', methods=['GET'])
    def health():
        return jsonify({"status": "ok"})

    return app


if __name__ == '__main__':
    from loguru import logger

    port = int(os.getenv('EMBEDDING_SERVER_PORT', 8083))
    logger.info(f"[EmbeddingServer] 启动于 0.0.0.0:{port}")
    app = create_app()
    _get_model()
    app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
