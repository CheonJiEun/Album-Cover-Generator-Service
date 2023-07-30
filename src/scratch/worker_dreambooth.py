# Python Built-in modules
import os
import io
import base64

# Pytorch
import torch
from torch import cuda

# ETC
from PIL import Image
import numpy as np
from numpy import random
from datetime import datetime
from pytz import timezone

# Celery
from celery import Celery
from celery import signals

# User Defined modules
from .gpt3_api import get_description
from .gcp.cloud_storage import GCSUploader
from .gcp.bigquery import BigQueryLogger
from .utils import load_yaml

# For Dreambooth
import subprocess
import random
import string
import urllib3
from pathlib import Path
from huggingface_hub import login

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

gcp_config = load_yaml(os.path.join("src/scratch/config", "private.yaml"), "gcp")
redis_config = load_yaml(os.path.join("src/scratch/config", "private.yaml"), "redis")
celery_config = load_yaml(os.path.join("src/scratch/config", "public.yaml"), "celery")
public_config = load_yaml(os.path.join("src/scratch/config", "public.yaml"))
huggingface_config = load_yaml(
    os.path.join("src/scratch/config", "private.yaml"), "huggingface"
)

bigquery_logger = BigQueryLogger(gcp_config)
gcs_uploader = GCSUploader(gcp_config)
login(token=huggingface_config["token"], add_to_git_credential=True)

# Initialize Celery
celery_app = Celery(
    "tasks",
    broker=redis_config["redis_server_ip"],
    backend=redis_config["redis_server_ip"],
)
celery_app.conf.worker_pool = "solo"

device = "cuda" if cuda.is_available() else "cpu"


@celery_app.task(name="save_image")
def save_image(filename, image_content, token):
    # Define the directory where to save the image
    image_dir = Path("src/scratch/dreambooth/data/users") / token
    # Create the directory if it does not exist
    image_dir.mkdir(parents=True, exist_ok=True)

    # Decode the image file content
    image_bytes = base64.b64decode(image_content)

    # Save the image locally
    with open(image_dir / filename, "wb") as buffer:
        buffer.write(image_bytes)

    return {"image_url": str(image_dir / filename)}


@celery_app.task(name="train_inference")
def train_inference(user, album, token):
    try:
        global model
        del model
    except:
        pass
    torch.cuda.empty_cache()

    seeds = np.random.randint(100000)
    # Run the train.py script as a separate process
    process = subprocess.Popen(
        [
            "python",
            "src/scratch/dreambooth/run.py",
            "--config-file",
            "src/scratch/dreambooth/dreambooth.yaml",
            "--token",
            token,
            "--user-gender",
            user["gender"],
            "--seed",
            str(seeds),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    stdout, stderr = process.communicate()

    if process.returncode != 0:
        return {"status": "error", "message": stderr.decode()}
    command = stdout.decode()

    os.chdir("src/scratch")
    subprocess.run(command, shell=True)
    os.chdir("/opt/ml/input/code/level3_cv_finalproject-cv-03")

    summarization = get_dreambooth_prompt(
        album["lyric"],
        album["album_name"],
        album["song_names"],
        user["gender"],
        album["genre"],
        album["artist_name"],
    )

    prompt = f"A image of a {album['genre']} music album cover that visualizes a {summarization} atmoshpere.\
        a {token} {user['gender']} is in image."

    # Run the train.py script as a separate process
    process = subprocess.Popen(
        [
            "python",
            "src/scratch/dreambooth/inference.py",
            "--token",
            token,
            "--prompt",
            prompt,
            "--user-gender",
            user["gender"],
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    # Get the output and error messages from the process
    stdout, stderr = process.communicate()

    # Check if the process completed successfully
    if process.returncode != 0:
        return {"status": "error", "message": stderr.decode()}

    saved_dir = f"src/scratch/dreambooth/data/results/{token}"

    files = os.listdir(saved_dir)
    urls = []

    for file in files:
        # Only process image files (png, jpg, etc.)
        if file.endswith((".png", ".jpg", ".jpeg")):
            # Open image file in binary mode and read it
            with open(os.path.join(saved_dir, file), "rb") as image_file:
                byte_arr = image_file.read()

            blob_name = f"{token}/{file}"
            # Append the tuple (byte_arr, desired_blob_name) to urls
            urls.append((byte_arr, blob_name))

    # Upload the images to GCS and get their URLs
    image_urls = gcs_uploader.save_image_to_gcs(urls)

    input_log = {
        "input_id": request_id,
        "user_id": input.user_id,
        "model": input.model,
        "song_name": input.song_name,
        "artist_name": input.artist_name,
        "album_name": input.album_name,
        "genre": input.genre,
        "lyric": input.lyric,
        "gender": input.gender,
        "image_urls": input.image_urls,
        "create_date": datetime.utcnow().astimezone(timezone("Asia/Seoul")).isoformat(),
    }
    bigquery_logger.log(input_log, "input")

    # Log to BigQuery
    output_id = str(uuid.uuid4())
    output_log = {
        "output_id": output_id,
        "input_id": request_id,
        "image_urls": image_urls,
        "seeds": [],  # TODO: 드림부스는 inference할때 시드값이 없나요?
        "prompt": prompt,
        "create_date": datetime.utcnow().astimezone(timezone("Asia/Seoul")).isoformat(),
    }
    bigquery_logger.log(output_log, "output")

    return {"images": image_urls, "output_id": output_id}


if __name__ == "__main__":
    celery_app.worker_main(["-l", "info"])
