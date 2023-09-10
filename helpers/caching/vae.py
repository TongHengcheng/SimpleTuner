import os, torch, logging
from tqdm import tqdm
from PIL import Image
from helpers.multiaspect.image import MultiaspectImage
from helpers.data_backend.base import BaseDataBackend
from helpers.data_backend.aws import S3DataBackend

logger = logging.getLogger("VAECache")
logger.setLevel(os.environ.get("SIMPLETUNER_LOG_LEVEL") or "INFO")


class VAECache:
    def __init__(
        self,
        vae,
        accelerator,
        data_backend: BaseDataBackend,
        cache_dir="vae_cache",
        resolution: int = 1024,
        delete_problematic_images: bool = False,
        write_batch_size: int = 25,
    ):
        self.data_backend = data_backend
        self.vae = vae
        self.vae.enable_slicing()
        self.accelerator = accelerator
        self.cache_dir = cache_dir
        self.resolution = resolution
        self.data_backend.create_directory(self.cache_dir)
        self.delete_problematic_images = delete_problematic_images
        self.write_batch_size = write_batch_size

    def _generate_filename(self, filepath: str) -> tuple:
        """Get the cache filename for a given image filepath and its base name."""
        # Extract the base name from the filepath and replace the image extension with .pt
        base_filename = os.path.splitext(os.path.basename(filepath))[0] + ".pt"
        full_filename = os.path.join(self.cache_dir, base_filename)
        return full_filename, base_filename

    def save_to_cache(self, filename, embeddings):
        self.data_backend.torch_save(embeddings, filename)

    def load_from_cache(self, filename):
        return self.data_backend.torch_load(filename)

    def discover_unprocessed_files(self, directory):
        """Identify files that haven't been processed yet."""
        all_files = {
            os.path.join(subdir, file)
            for subdir, _, files in self.data_backend.list_files(
                "*.[jJpP][pPnN][gG]", directory
            )
            for file in files
            if file.endswith((".png", ".jpg", ".jpeg"))
        }
        processed_files = {self._generate_filename(file) for file in all_files}
        unprocessed_files = {
            file
            for file in all_files
            if self._generate_filename(file) not in processed_files
        }
        return list(unprocessed_files)

    def encode_image(self, pixel_values, filepath):
        full_filenames, latents = self.encode_image_batch([pixel_values], [filepath])
        return latents[0]

    def encode_image_batch(self, pixel_values_batch, filepaths):
        # Initialize lists to store filenames and latent vectors
        full_filenames = []
        latents_list = []

        # Convert the list of pixel_values to a single tensor for batch processing
        pixel_values_tensor = torch.stack(pixel_values_batch).to(
            self.accelerator.device, dtype=self.vae.dtype
        )

        # Perform batch encoding using VAE
        with torch.no_grad():
            latent_distributions = self.vae.encode(pixel_values_tensor)
            latents = latent_distributions.latent_dist.sample()

        # Rescale latents if necessary
        latents = latents * self.vae.config.scaling_factor

        # Iterate through each filepath to generate filenames and check cache
        for i, filepath in enumerate(filepaths):
            full_filename, base_filename = self._generate_filename(filepath)
            if self.data_backend.exists(full_filename):
                latents[i] = self.load_from_cache(full_filename)

            # Append to lists
            full_filenames.append(full_filename)
            latents_list.append(
                latents[i].squeeze().to(self.accelerator.device, dtype=self.vae.dtype)
            )

        return full_filenames, torch.stack(latents_list)

    def split_cache_between_processes(self):
        all_unprocessed_files = self.discover_unprocessed_files(self.cache_dir)
        # Use the accelerator to split the data
        with self.accelerator.split_between_processes(
            all_unprocessed_files
        ) as split_files:
            self.local_unprocessed_files = split_files

    def process_directory(self, directory):
        # Define a transform to convert the image to tensor
        transform = MultiaspectImage.get_image_transforms()

        # Get a list of all existing .pt files in the directory
        existing_pt_files = set()
        logger.debug(f"Retrieving list of pytorch cache files from {self.cache_dir}")
        remote_cache_list = self.data_backend.list_files(
            instance_data_root=self.cache_dir, str_pattern="*.pt"
        )
        for subdir, _, files in remote_cache_list:
            for file in files:
                if subdir != "":
                    file = os.path.join(subdir, file)
                existing_pt_files.add(os.path.splitext(file)[0])
        # Get a list of all the files to process (customize as needed)
        files_to_process = self.local_unprocessed_files  # Use the local slice of files
        target_name = directory
        if type(self.data_backend) == S3DataBackend:
            target_name = f"S3 bucket {self.data_backend.bucket_name}"
        logger.debug(f"Beginning processing of VAECache source data {target_name}")
        all_image_files = self.data_backend.list_files(
            instance_data_root=directory, str_pattern="*.[jJpP][pPnN][gG]"
        )
        for subdir, _, files in all_image_files:
            for file in files:
                # If processed file already exists, skip processing for this image
                if os.path.splitext(file)[0] in existing_pt_files:
                    continue
                files_to_process.append(os.path.join(subdir, file))

        # Shuffle the files.
        import random

        random.shuffle(files_to_process)

        # Iterate through the files, displaying a progress bar
        current_batch_filepaths = []
        batch_pixel_values = []
        for filepath in tqdm(files_to_process, desc="Processing images"):
            # Create a hash based on the filename
            full_filename, base_filename = self._generate_filename(filepath)

            # If processed file already exists in cache, skip processing for this image
            if self.data_backend.exists(full_filename):
                continue

            try:
                image = self.data_backend.read_image(filepath)
                image = MultiaspectImage.prepare_image(image, self.resolution)
                pixel_values = transform(image).to(
                    self.accelerator.device, dtype=self.vae.dtype
                )
                current_batch_filepaths.append(filepath)
                batch_pixel_values.append(pixel_values)
            except (OSError, RuntimeError) as e:
                logger.error(f"Encountered error: {e}")
                if self.delete_problematic_images:
                    self.data_backend.delete(filepath)
                continue

            # If batch size is reached, process the batch
            if len(batch_pixel_values) == self.write_batch_size:
                full_filenames, batch_latents = self.encode_image_batch(
                    batch_pixel_values, current_batch_filepaths
                )
                self.data_backend.write_batch(full_filenames, batch_latents)
                current_batch_filepaths.clear()
                batch_pixel_values.clear()

        # Process any remaining items in batch_pixel_values
        if batch_pixel_values:
            full_filenames, batch_latents = self.encode_image_batch(
                batch_pixel_values, files_to_process
            )
            self.data_backend.write_batch(full_filenames, batch_latents)
