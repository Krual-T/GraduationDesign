import click

@click.command()
@click.option('--mode', type=click.Choice(['train-valid', 'test']), default='train-valid', help='运行模式')
def run(mode):
    print(f"🚀 当前运行模式：{mode}")
    from src.trains.trainers.compatibility_prediction_trainer import CompatibilityPredictionTrainer
    with CompatibilityPredictionTrainer(run_mode=mode) as cp_trainer:
        cp_trainer.run()

if __name__ == '__main__':
    run()
