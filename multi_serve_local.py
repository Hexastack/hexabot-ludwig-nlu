import argparse
import asyncio
import io
import json
import logging
import os
import sys
import tempfile
from typing import Dict, Optional, Tuple, Union, List

import pandas as pd
import torch
from torchvision.io import decode_image

from ludwig.api import LudwigModel
from ludwig.constants import AUDIO, COLUMN
from ludwig.contrib import add_contrib_callback_args
from ludwig.globals import LUDWIG_VERSION
from ludwig.utils.print_utils import get_logging_level_registry, print_ludwig
from ludwig.utils.server_utils import NumpyJSONResponse
from ludwig.backend import Backend
from ludwig.callbacks import Callback

logger = logging.getLogger(__name__)

try:
    import uvicorn
    from fastapi import FastAPI, status
    from starlette.datastructures import UploadFile
    from starlette.middleware import Middleware
    from starlette.middleware.cors import CORSMiddleware
    from starlette.requests import Request
except ImportError as e:
    logger.error(e)
    logger.error(
        " fastapi and other serving dependencies cannot be loaded"
        "and may have not been installed. "
        "In order to install all serving dependencies run "
        "pip install ludwig[serve]"
    )
    sys.exit(-1)

ALL_FEATURES_PRESENT_ERROR = {"error": "entry must contain all input features"}

COULD_NOT_RUN_INFERENCE_ERROR = {"error": "Unexpected Error: could not run inference on model"}

AUTH_TOKEN = os.getenv("AUTH_TOKEN", "TOKEN_MUST_BE_DEFINED")

def server(models, allowed_origins=None):
    middleware = [Middleware(CORSMiddleware, allow_origins=allowed_origins)] if allowed_origins else None
    app = FastAPI(middleware=middleware)

    @app.get("/")
    def check_health():
        return NumpyJSONResponse({"message": "Ludwig server is up", "models": list(models.keys())})
        
    
    @app.post("/predict")
    async def predict(request: Request):
        try:
            # Parse form data
            form = await request.form()
            model_names = form.get("model")  # Single model or list of models
            model_names = model_names.split(",") if model_names else None
            files=[]
        except Exception:
            logger.exception("Failed to parse predict form")
            return NumpyJSONResponse(COULD_NOT_RUN_INFERENCE_ERROR, status_code=500)

        async def predict_by_model(model_name: str, model: LudwigModel) -> dict:
            try:
                entry, files = convert_input(form, model.model.input_features)  # Input compatible with all models
                input_features = {f[COLUMN] for f in model.config["input_features"]}
                if (entry.keys() & input_features) != input_features:
                    missing_features = set(input_features) - set(entry.keys())
                    return {
                        "model": model_name,
                        "response": {
                            "error": f"Missing features: {missing_features}.",
                            "status": "failed",
                        },
                    }
                resp, _ = model.predict(dataset=[entry], data_format=dict)
                return {
                    "model": model_name,
                    "response": {
                        "predictions": resp.to_dict("records")[0],
                        "status": "success",
                    },
                }
            except Exception as exc:
                logger.exception(f"Failed to predict for model '{model_name}': {exc}")
                return {
                    "model": model_name,
                    "response": {
                        "error": str(exc),
                        "status": "failed",
                    },
                }

        try:
            # Determine target models
            if model_names:
                invalid_models = [name for name in model_names if name not in models]
                if invalid_models:
                    return NumpyJSONResponse(
                        {"error": f"Invalid model names: {invalid_models}. Available models: {list(models.keys())}."},
                        status_code=400,
                    )
                target_models = {name: models[name] for name in model_names}
            else:  
                # Predict for all models if no specific model(s) are provided
                target_models = models

            # Run 
            tasks = [predict_by_model(name, model) for name, model in target_models.items()]
            results = await asyncio.gather(*tasks)
            responses = {result["model"]: result["response"] for result in results}
            return NumpyJSONResponse(responses)
        except Exception:
            logger.exception("Failed to execute predictions")
            return NumpyJSONResponse(COULD_NOT_RUN_INFERENCE_ERROR, status_code=500)
        finally:
            for f in files:
                os.remove(f.name)

    @app.post("/batch_predict")
    async def batch_predict(request: Request):
        try:
            # Parse form data
            form = await request.form()
            model_names = form.get("model")  # Single model or list of models
            model_names = model_names.split(",") if model_names else None
            files=[]
           
        except Exception:
            logger.exception("Failed to parse batch_predict form")
            return NumpyJSONResponse(COULD_NOT_RUN_INFERENCE_ERROR, status_code=500)

        async def batch_predict_by_model(model_name:str , model: LudwigModel) -> dict:
            try:
                data, files = convert_batch_input(form, model.model.input_features) 
                data_df = pd.DataFrame.from_records(data["data"], index=data.get("index"), columns=data["columns"])
                input_features = {f[COLUMN] for f in model.config["input_features"]}

                if (set(data_df.columns) & input_features) != input_features:
                    missing_features = set(input_features) - set(data_df.columns)
                    return {
                        "model": model_name,
                        "response": {
                            "error": f"Missing features: {missing_features}.",
                            "status": "failed",
                        },
                    }
                resp, _ = model.predict(dataset=data_df)
                return {
                    "model": model_name,
                    "response": {
                        "predictions": resp.to_dict("split"),
                        "status": "success",
                    },
                }
            except Exception as exc:
                logger.exception(f"Failed to batch predict for model '{model_name}': {exc}")
                return {
                    "model": model_name,
                    "response": {
                        "error": str(exc),
                        "status": "failed",
                    },
                }

        try:
            # Determine target models
            if model_names:
                invalid_models = [name for name in model_names if name not in models]
                if invalid_models:
                    return NumpyJSONResponse(
                        {"error": f"Invalid model names: {invalid_models}. Available models: {list(models.keys())}."},
                        status_code=400,
                    )
                target_models = {name: models[name] for name in model_names}
            else:  # Predict for all models if no specific model(s) are provided
                target_models = models

            # Run batch predictions
            tasks = [batch_predict_by_model(name, model) for name, model in target_models.items()]
            results = await asyncio.gather(*tasks)
            responses = {result["model"]: result["response"] for result in results}
            return NumpyJSONResponse(responses)
        except Exception:
            logger.exception("Failed to execute batch predictions")
            return NumpyJSONResponse(COULD_NOT_RUN_INFERENCE_ERROR, status_code=500)
        finally:
            for f in files:
                os.remove(f.name)
    return app

def _write_file(v, files):
    # Convert UploadFile to a NamedTemporaryFile to ensure it's on the disk
    suffix = os.path.splitext(v.filename)[1]
    named_file = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    files.append(named_file)
    named_file.write(v.file.read())
    named_file.close()
    return named_file.name


def _read_image_buffer(v):
    # read bytes sent via REST API and convert to image tensor
    # in [channels, height, width] format
    byte_string = io.BytesIO(v.file.read()).read()
    image = decode_image(torch.frombuffer(byte_string, dtype=torch.uint8))
    return image  # channels, height, width


def convert_input(form, input_features):
    """Returns a new input and a list of files to be cleaned up."""
    new_input = {}
    files = []
    for k, v in form.multi_items():
        if isinstance(v, UploadFile):
            # check if audio or image file
            if input_features.get(k).type() == AUDIO:
                new_input[k] = _write_file(v, files)
            else:
                new_input[k] = _read_image_buffer(v)
        else:
            new_input[k] = v

    return new_input, files


def convert_batch_input(form, input_features):
    """Returns a new input and a list of files to be cleaned up."""
    file_index = {}
    files = []
    for k, v in form.multi_items():
        if isinstance(v, UploadFile):
            file_index[v.filename] = v

    data = json.loads(form["dataset"])
    for row in data["data"]:
        for i, value in enumerate(row):
            if value in file_index:
                feature_name = data["columns"][i]
                if input_features.get(feature_name).type() == AUDIO:
                    row[i] = _write_file(file_index[value], files)
                else:
                    row[i] = _read_image_buffer(file_index[value])

    return data, files

async def are_models_loaded(models: Dict[str, LudwigModel]) -> bool:
    # Implement a check to verify all models are fully loaded
    return all(model is not None for model in models.values())

def run_server(
    model_paths: dict,  # Dictionary of model IDs to paths
    host: str,
    port: int,
    allowed_origins: list,
) -> None:
    """Loads pre-trained models and serves them on an http server."""
    # If model_paths is a string, convert it to a dictionary
    if isinstance(model_paths, str):
        model_paths = json.loads(model_paths)
    
    models = {}
    for model_name, model_path in model_paths.items():
        models[model_name] = LudwigModel.load(model_path, backend="local")
    
    # Check if models are loaded
    if not asyncio.run(are_models_loaded(models)):
        headers = {"Retry-After": "120"}  # Suggest retrying after 2 minutes
        response = NumpyJSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"message": "Models are still loading, please retry later."},
            headers=headers,
        )
        return response

    app = server(models, allowed_origins)
    uvicorn.run(app, host=host, port=port)


def cli(sys_argv):
    parser = argparse.ArgumentParser(
        description="This script serves multiple locally trained models", prog="ludwig multi_serve_local", usage="%(prog)s [options]"
    )

    # ----------------
    # Model parameters
    # ----------------
    parser.add_argument("-m", "--model_paths", help="model to load", required=True)

    parser.add_argument(
        "-l",
        "--logging_level",
        default="info",
        help="the level of logging to use",
        choices=["critical", "error", "warning", "info", "debug", "notset"],
    )

    # ----------------
    # Server parameters
    # ----------------
    parser.add_argument(
        "-p",
        "--port",
        help="port for server (default: 8000)",
        default=8000,
        type=int,
    )

    parser.add_argument("-H", "--host", help="host for server (default: 0.0.0.0)", default="0.0.0.0")

    parser.add_argument(
        "-ao",
        "--allowed_origins",
        nargs="*",
        help="A list of origins that should be permitted to make cross-origin requests. "
        'Use "*" to allow any origin. See https://www.starlette.io/middleware/#corsmiddleware.',
    )

    add_contrib_callback_args(parser)
    args = parser.parse_args(sys_argv)

    args.callbacks = args.callbacks or []
    for callback in args.callbacks:
        callback.on_cmdline("serve", *sys_argv)

    args.logging_level = get_logging_level_registry()[args.logging_level]
    logging.getLogger("ludwig").setLevel(args.logging_level)
    global logger
    logger = logging.getLogger("ludwig.serve")

    print_ludwig("Serve", LUDWIG_VERSION)

    run_server(args.model_paths, args.host, args.port, args.allowed_origins)


if __name__ == "__main__":
    cli(sys.argv[1:])