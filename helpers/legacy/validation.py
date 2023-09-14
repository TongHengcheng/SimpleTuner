import logging, torch, numpy as np

from diffusers.utils import is_wandb_available

if is_wandb_available():
    import wandb

from diffusers import DPMSolverMultistepScheduler, DiffusionPipeline


def prepare_validation_prompt_list(args, embed_cache):
    validation_negative_prompt_embeds = None
    validation_negative_pooled_embeds = None
    validation_prompts = []
    validation_shortnames = []
    if not hasattr(embed_cache, "model_type"):
        return ValueError(
            f"Embed cache engine did not contain a model_type. Cannot continue."
        )
    model_type = embed_cache.model_type
    if args.validation_prompt_library:
        # Use the SimpleTuner prompts library for validation prompts.
        from helpers.prompts import prompts as prompt_library

        # Iterate through the prompts with a progress bar
        for shortname, prompt in tqdm(
            prompt_library.items(), desc="Precomputing validation prompt embeddings"
        ):
            embed_cache.compute_embeddings_for_prompts([prompt])
            validation_prompts.append(prompt)
            validation_shortnames.append(shortname)
    if args.user_prompt_library is not None:
        user_prompt_library = PromptHandler.load_user_prompts(args.user_prompt_library)
        for shortname, prompt in tqdm(
            user_prompt_library.items(),
            desc="Precomputing user prompt library embeddings",
        ):
            embed_cache.compute_embeddings_for_prompts([prompt])
            validation_prompts.append(prompt)
            validation_shortnames.append(shortname)
    if args.validation_prompt is not None:
        # Use a single prompt for validation.
        # This will add a single prompt to the prompt library, if in use.
        validation_prompts = validation_prompts + [args.validation_prompt]
        validation_shortnames = validation_shortnames + ["validation"]

    # Compute negative embed for validation prompts, if any are set.
    if validation_prompts:
        (
            validation_negative_prompt_embeds,
            validation_negative_pooled_embeds,
        ) = embed_cache.compute_embeddings_for_prompts(["blurry, cropped, ugly"])
    if model_type == "sdxl":
        return (
            validation_prompts,
            validation_shortnames,
            validation_negative_prompt_embeds,
            validation_negative_pooled_embeds,
        )
    elif model_type == "legacy":
        return (validation_prompts, validation_shortnames)


def log_validation(
    validation_prompts: list,
    text_encoder,
    tokenizer,
    unet,
    vae,
    args,
    accelerator,
    weight_dtype,
    epoch,
):
    logging.info(
        f"Running validation... \n Generating {args.num_validation_images} images with prompt:"
        f" {args.validation_prompt}."
    )
    # create pipeline (note: unet and vae are loaded again in float32)
    pipeline = DiffusionPipeline.from_pretrained(
        args.pretrained_model_name_or_path,
        text_encoder=accelerator.unwrap_model(text_encoder),
        tokenizer=tokenizer,
        unet=accelerator.unwrap_model(unet),
        vae=vae,
        revision=args.revision,
        torch_dtype=weight_dtype,
    )
    pipeline.scheduler = DPMSolverMultistepScheduler.from_config(
        pipeline.scheduler.config
    )
    pipeline.unet = torch.compile(pipeline.unet)
    # pipeline = pipeline.to(accelerator.device)
    pipeline.set_progress_bar_config(disable=True)

    # run inference
    generator = (
        None
        if args.seed is None
        else torch.Generator(device=accelerator.device).manual_seed(args.seed)
    )
    images = []
    for _ in range(args.num_validation_images):
        with torch.autocast("cuda"):
            negative = "negative low quality, low res, messy, grainy, smooth, sand, big eyes, anime, fractured, cracked, wrinkles, makeup (deformed, distorted, disfigured:1.3), poorly drawn, bad anatomy, wrong anatomy, extra limb, missing limb, floating limbs, (mutated hands and fingers:1.4), disconnected limbs, mutation, mutated, ugly, disgusting, blurry, amputation, synthetic, rendering"
            image = pipeline(
                args.validation_prompt,
                negative_prompt=negative,
                num_inference_steps=35,
                generator=generator,
            ).images[0]
        images.append(image)

    for tracker in accelerator.trackers:
        if tracker.name == "tensorboard":
            np_images = np.stack([np.asarray(img) for img in images])
            tracker.writer.add_images(
                "validation", np_images, epoch, dataformats="NHWC"
            )
        if tracker.name == "wandb":
            tracker.log(
                {
                    "validation": [
                        wandb.Image(image, caption=f"{i}: {args.validation_prompt}")
                        for i, image in enumerate(images)
                    ]
                }
            )

    del pipeline
    torch.cuda.empty_cache()

    return images


def validation_logs(
    logger,
    accelerator,
    unet,
    args,
    validation_prompts,
    global_step,
    resume_global_step,
    progress_bar,
    text_encoder_1,
    tokenizer,
    vae_path: str,
    weight_dtype,
    embed_cache,
    validation_negative_pooled_embeds,
    text_encoder_2=None,
    tokenizer_2=None,
    ema_unet=None,
    vae=None,
):
    ### BEGIN: Perform validation every `validation_epochs` steps
    if accelerator.is_main_process:
        if (
            validation_prompts
            and global_step % args.validation_steps == 0
            and progress_bar.n > resume_global_step
        ):
            logger.debug(
                f"We might want to process validations, because we have {len(validation_prompts)} validation prompts,"
                f" and we are on step {global_step} which meshes with our specified interval of {args.validation_steps} steps."
            )
            if (
                validation_prompts is None
                or validation_prompts == []
                or args.num_validation_images is None
                or args.num_validation_images <= 0
            ):
                logger.warning(
                    f"Not generating any validation images for this checkpoint. Live dangerously and prosper, pal!"
                )
                continue
            logger.debug(
                f"We have valid prompts to process, this is looking better for our decision tree.."
            )
            if (
                args.gradient_accumulation_steps > 0
                and step % args.gradient_accumulation_steps != 0
            ):
                # We do not want to perform validation on a partial batch.
                logger.debug(
                    f"Not producing a validation batch for {args.gradient_accumulation_steps} gradient accumulation steps vs {step} step count. We are at a partial batch."
                )
                continue
            logger.info(
                f"Running validation... \n Generating {len(validation_prompts)} images."
            )
            # create pipeline
            if args.use_ema:
                # Store the UNet parameters temporarily and load the EMA parameters to perform inference.
                ema_unet.store(unet.parameters())
                ema_unet.copy_to(unet.parameters())
            if vae is None:
                vae = AutoencoderKL.from_pretrained(
                    vae_path,
                    subfolder="vae"
                    if args.pretrained_vae_model_name_or_path is None
                    else None,
                    revision=args.revision,
                    force_upcast=False,
                )
            # The models need unwrapping because for compatibility in distributed training mode.
            pipeline = StableDiffusionXLPipeline.from_pretrained(
                args.pretrained_model_name_or_path,
                unet=accelerator.unwrap_model(unet),
                text_encoder=text_encoder_1,
                text_encoder_2=text_encoder_2,
                tokenizer=None,
                tokenizer_2=None,
                vae=vae,
                revision=args.revision,
                torch_dtype=weight_dtype,
            )
            pipeline.scheduler = DDIMScheduler.from_pretrained(
                args.pretrained_model_name_or_path,
                subfolder="scheduler",
                prediction_type=args.prediction_type,
                timestep_spacing=args.inference_scheduler_timestep_spacing,
                rescale_betas_zero_snr=args.rescale_betas_zero_snr,
            )
            pipeline = pipeline.to(accelerator.device)
            pipeline.set_progress_bar_config(disable=True)

            # run inference
            # Save validation images
            val_save_dir = os.path.join(args.output_dir, "validation_images")
            if not os.path.exists(val_save_dir):
                os.makedirs(val_save_dir)

            with torch.autocast(
                str(accelerator.device).replace(":0", ""),
                enabled=(
                    accelerator.mixed_precision == "fp16"
                    or accelerator.mixed_precision == "bf16"
                ),
            ):
                validation_images = []
                pipeline = pipeline.to(accelerator.device)
                extra_validation_kwargs = {}
                with torch.autocast(str(accelerator.device).replace(":0", "")):
                    if not args.validation_randomize:
                        extra_validation_kwargs["generator"] = torch.Generator(
                            device=accelerator.device
                        ).manual_seed(args.validation_seed or args.seed or 0)
                    for validation_prompt in tqdm(
                        validation_prompts, desc="Generating validation images"
                    ):
                        # Each validation prompt needs its own embed.
                        (
                            current_validation_prompt_embeds,
                            current_validation_pooled_embeds,
                        ) = embed_cache.compute_embeddings_for_prompts(
                            [validation_prompt]
                        )
                        logger.debug(
                            f"Generating validation image: {validation_prompt}"
                        )
                        validation_images.extend(
                            pipeline(
                                prompt_embeds=current_validation_prompt_embeds,
                                pooled_prompt_embeds=current_validation_pooled_embeds,
                                negative_prompt_embeds=validation_negative_prompt_embeds,
                                negative_pooled_prompt_embeds=validation_negative_pooled_embeds,
                                num_images_per_prompt=args.num_validation_images,
                                num_inference_steps=30,
                                guidance_scale=args.validation_guidance,
                                guidance_rescale=args.validation_guidance_rescale,
                                height=args.validation_resolution,
                                width=args.validation_resolution,
                                **extra_validation_kwargs,
                            ).images
                        )

                for tracker in accelerator.trackers:
                    if tracker.name == "wandb":
                        validation_document = {}
                        validation_luminance = []
                        for idx, validation_image in enumerate(validation_images):
                            # Create a WandB entry containing each image.
                            validation_document[
                                validation_shortnames[idx]
                            ] = wandb.Image(validation_image)
                            validation_luminance.append(
                                calculate_luminance(validation_image)
                            )
                        # Compute the mean luminance across all samples:
                        validation_luminance = torch.tensor(validation_luminance)
                        validation_document[
                            "validation_luminance"
                        ] = validation_luminance.mean()
                        del validation_luminance
                        tracker.log(validation_document, step=global_step)
                val_img_idx = 0
                for a_val_img in validation_images:
                    a_val_img.save(
                        os.path.join(
                            val_save_dir,
                            f"step_{global_step}_val_img_{val_img_idx}.png",
                        )
                    )
                    val_img_idx += 1

            if args.use_ema:
                # Switch back to the original UNet parameters.
                ema_unet.restore(unet.parameters())
            if not args.keep_vae_loaded:
                del vae
                vae = None
            del pipeline
            torch.cuda.empty_cache()
        ### END: Perform validation every `validation_epochs` steps
