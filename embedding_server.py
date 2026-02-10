"""
独立 Embedding 服务 - 单进程、只加载一次模型，供主服务通过 HTTP 调用。
启动: python embedding_server.py
环境变量: EMBEDDING_MODEL, EMBEDDING_DEVICE, EMBEDDING_BATCH_SIZE, EMBEDDING_SERVER_PORT
"""
import os
import time

from dotenv import load_dotenv
load_dotenv()

# 在进程内只加载一次模型（模块级单例）
_model = None
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
