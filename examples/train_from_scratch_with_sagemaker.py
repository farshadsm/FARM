import json
import logging
from pathlib import Path

from transformers.tokenization_bert import BertTokenizer

from farm.data_handler.data_silo import StreamingDataSilo
from farm.data_handler.processor import BertStyleLMProcessor
from farm.modeling.adaptive_model import AdaptiveModel
from farm.modeling.language_model import LanguageModel
from farm.modeling.optimization import initialize_optimizer
from farm.modeling.prediction_head import BertLMHead, NextSentenceHead
from farm.train import Trainer
from farm.utils import set_all_seeds, StdoutLogger, initialize_device_settings
import argparse

def parse_arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on GPUs")
    args, unknownargs = parser.parse_known_args()
    logging.info(f"Got unknown arguments: {unknownargs}")
    return args


def train_from_scratch(args):
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s -   %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        level=logging.INFO,
    )

    #TODO prettify this loading of params from two sources (cmd + json)
    cmd_args = parse_arguments()
    args["local_rank"] = cmd_args.local_rank
    logging.info(f'local_rank: {args["local_rank"]}')

    # Only the main process should log here
    if args["local_rank"] in [-1, 0]:
        ml_logger = StdoutLogger(tracking_uri=None)
        ml_logger.init_experiment(experiment_name="train_from_scratch", run_name="run")

    set_all_seeds(seed=39)

    device, n_gpu = initialize_device_settings(use_cuda=True, local_rank=args["local_rank"], use_amp=args.get("use_amp",None))

    distributed = args.local_rank != -1

    save_dir = Path("/opt/ml/model")
    data_dir = Path("/opt/ml/input/data/input_channel")

    # 1.Create a tokenizer
    tokenizer = BertTokenizer(data_dir/args["vocab_file"], do_lower_case=bool(int(args["do_lower_case"])))

    # 2. Create a DataProcessor that handles all the conversion from raw text into a PyTorch Dataset
    processor = BertStyleLMProcessor(
        data_dir=data_dir,
        tokenizer=tokenizer, max_seq_len=int(args["max_seq_len"]),
        train_filename=args["train_file"],
        dev_filename=args.get("dev_file", None),
        test_filename=args.get("test_file", None),
        next_sent_pred_style=args.get("next_sent_pred_style", "bert-style"),
        max_docs=args.get("max_docs", None)
    )

    # 3. Create a DataSilo that loads several datasets (train/dev/test), provides DataLoaders for them and
    #    calculates a few descriptive statistics of our datasets
    stream_data_silo = StreamingDataSilo(processor=processor, batch_size=int(args["batch_size"]),
                                         dataloader_workers=int(args.get("data_loader_workers", 8)),
                                         distributed=distributed)

    # 4. Create an AdaptiveModel
    # a) which consists of a pretrained language model as a basis
    language_model = LanguageModel.from_scratch("bert", tokenizer.vocab_size)

    # b) and *two* prediction heads on top that are suited for our task => Language Model finetuning
    lm_prediction_head = BertLMHead(768, tokenizer.vocab_size)
    next_sentence_head = NextSentenceHead(num_labels=2, task_name="nextsentence")

    model = AdaptiveModel(
        language_model=language_model,
        prediction_heads=[lm_prediction_head, next_sentence_head],
        embeds_dropout_prob=0.1,
        lm_output_types=["per_token", "per_sequence"],
        device=device,
    )

    # 5. Create an optimizer
    model, optimizer, lr_schedule = initialize_optimizer(
        model=model,
        learning_rate=float(args["learning_rate"]),
        schedule_opts={"name": "LinearWarmup", "warmup_proportion": float(args["warmup_proportion"])},
        n_batches=len(stream_data_silo.get_data_loader("train")),
        n_epochs=int(args["n_epochs"]),
        device=device,
        grad_acc_steps=int(args["gradient_accumulation_steps"]),
        distributed=distributed,
        use_amp=args.get("use_amp",None),
        local_rank=args["local_rank"]
    )

    # 6. Feed everything to the Trainer, which keeps care of growing our model and evaluates it from time to time
    if args.get("checkpoint_every"):
        checkpoint_every = int(args["checkpoint_every"])
        checkpoint_root_dir = Path("/opt/ml/checkpoints/training")
    else:
        checkpoint_every = None
        checkpoint_root_dir = None

    trainer = Trainer.create_or_load_checkpoint(
        model=model,
        optimizer=optimizer,
        data_silo=stream_data_silo,
        epochs=int(args["n_epochs"]),
        n_gpu=n_gpu,
        lr_schedule=lr_schedule,
        evaluate_every=int(args["evaluate_every"]),
        log_loss_every=int(args["log_loss_every"], 500),
        device=device,
        local_rank=args["local_rank"],
        grad_acc_steps=int(args["gradient_accumulation_steps"]),
        checkpoint_every=checkpoint_every,
        checkpoint_root_dir=checkpoint_root_dir,
        checkpoints_to_keep=int(args.get("checkpoints_to_keep", 10)),
        disable_tqdm=True,
        use_amp=args.get("use_amp", None),
    )

    # 7. Let it grow!
    trainer.train()

    # 8. Hooray! You have a model. Store it:
    model.save(save_dir)
    processor.save(save_dir)


if __name__ == "__main__":
    with open("/opt/ml/input/config/hyperparameters.json") as f:
        params = json.load(f)
    logging.info(f"Starting a train job with parameters {params}")
    train_from_scratch(params)
