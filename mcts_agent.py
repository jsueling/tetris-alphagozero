"""MCTS + DRL agent that learns how to play Tetris"""

from time import time

from tqdm import tqdm
import numpy as np

from experience_replay_buffer import ExperienceReplayBuffer
from tetris_env import Tetris
from model import A0ResNet
from score_normaliser import ScoreNormaliser
from checkpoint import Checkpoint

MCTS_ITERATIONS = 1600 # Number of MCTS iterations per action selection
ACTION_SPACE = 40 # Upper bound on possible actions for hard drop (rotations * columns placements)
BATCH_SIZE = 256 # Batch size for experience replay

class MCTSAgent:
    """MCTS + DRL agent for playing Tetris."""
    def __init__(
        self,
        checkpoint_name,
        batch_size=BATCH_SIZE,
        total_iterations=100,
        episodes_per_iteration=200,
        num_benchmark_episodes=50,
        min_buffer_size_for_update=1e3,
        n_workers=10
    ):
        self.model = A0ResNet(num_residual_blocks=19, num_actions=ACTION_SPACE)
        self.candidate_model = A0ResNet(num_residual_blocks=19, num_actions=ACTION_SPACE)
        self.buffer = ExperienceReplayBuffer(
            batch_size=batch_size,
            max_size=500000,
            device=self.model.device
        )
        self.env = Tetris()
        self.score_normaliser = ScoreNormaliser()
        self.checkpoint = Checkpoint(
            name=checkpoint_name,
            buffer=self.buffer,
            model=self.model,
            score_normaliser=self.score_normaliser
        )

        self.batch_size = batch_size
        # Number of workers for parallel MCTS execution
        self.n_workers = n_workers
        self.total_iterations = total_iterations
        self.episodes_per_iteration = episodes_per_iteration
        self.benchmark_episode_count = num_benchmark_episodes
        self.min_buffer_size_for_update = min_buffer_size_for_update
        self.max_benchmark_score = 0

    def update(self):
        """Update the candidate model using the best model's generated experience"""

        states, tree_policies, normalised_rtg, legal_actions_masks = self.buffer.sample()

        self.candidate_model.train()
        self.candidate_model.optimiser.zero_grad()
        policy_loss, value_loss = self.candidate_model.loss(
            states,
            tree_policies,
            normalised_rtg,
            legal_actions_masks
        )
        total_loss = policy_loss + value_loss
        total_loss.backward()
        self.candidate_model.optimiser.step()
        # Update LR of optimiser via scheduler step
        self.candidate_model.scheduler.step(total_loss)

        self.checkpoint.log_training_loss(
            policy_loss.detach().cpu().item(),
            value_loss.detach().cpu().item()
        )

    def train(self):
        """Run the training loop for the MCTS agent."""

        # Attempt to restore if there is a saved state
        self.checkpoint.restore_checkpoint()

        # Avoid duplicate benchmarking if there exists a previous benchmark score.
        if len(self.checkpoint.benchmark_scores) == 0:
            for _ in tqdm(range(self.benchmark_episode_count), desc="Benchmarking initial model"):
                final_score, _, _ = self.run_episode(self.model, benchmark=True)
                self.max_benchmark_score += final_score
        else:
            self.max_benchmark_score = self.checkpoint.benchmark_scores[-1]

        starting_iteration = self.checkpoint.completed_iterations

        for iter_idx in tqdm(
            range(starting_iteration, self.total_iterations),
            total=self.total_iterations,
            desc="Training MCTS agent",
            unit="iteration"
        ):

            transitions_added = 0

            for _ in range(self.episodes_per_iteration):

                # Begin episode
                start_time = time.time()

                final_score, transitions, step_count = self.run_episode(self.model, benchmark=False)

                # End episode
                self.checkpoint.log_episode_results(
                    start_time,
                    final_score,
                    step_count
                )

                self.process_transitions(transitions, final_score)
                transitions_added += len(transitions)

                # Adaptively interweaves updates with data generation
                if (len(self.buffer) > self.min_buffer_size_for_update) and \
                   (transitions_added >= self.batch_size):
                    self.update()
                    transitions_added -= self.batch_size

            # Use any remaining credit on updates (1:1 ratio of data generation to updates)
            while (len(self.buffer) > self.min_buffer_size_for_update) and \
                  (transitions_added >= self.batch_size):
                self.update()
                transitions_added -= self.batch_size

            # The first iteration is reserved for data generation
            if iter_idx > 0:
                # Evaluates the current model against the candidate model
                # and updates model and benchmark score if the candidate model
                # outperforms the current model.
                self.evaluate_models()

            self.checkpoint.save_iteration(self.model, self.max_benchmark_score)

    def process_transitions(self, transitions, final_score):
        """Process transitions after an episode ends."""

        states = np.stack([t[0] for t in transitions])
        tree_policies = np.stack([t[1] for t in transitions])
        legal_actions_masks = np.stack([t[2] for t in transitions])
        scores_before_action = np.stack([t[3] for t in transitions])

        # After each episode, calculate the return-to-go (RTG) and normalise
        # it to range [-1, 1] based on rolling average agent score since Tetris
        # scores are unbounded. This should encourage the agent to learn
        # to play moves (visit actions) that score better than its current iteration
        # when used in MCTS node selection in deciding which children to visit - Q(s,a).
        # The assumption is that moves that were improvements on average score in
        # the past should also be improvements on average score in the future which is a
        # reasonable assumption for Tetris due to the repeated nature of the game.

        rewards_to_go = final_score - scores_before_action
        # Calculate normalised rewards-to-go (RTG)
        normalised_rewards_to_go = self.score_normaliser.normalise(rewards_to_go)

        # Add all transitions to the experience replay buffer
        self.buffer.add_transitions_batch(
            states,
            tree_policies,
            normalised_rewards_to_go,
            legal_actions_masks
        )

        # Update the normalising factor per batch of rewards-to-go
        self.score_normaliser.update(rewards_to_go)

    def run_episode(self, model, benchmark=False):
        """
        This method runs a single episode of Tetris using MCTS to select actions.
        Benchmarking mode is used to evaluate the model's performance.
        Returns:
        - final_score: The final score of the episode.
        - transitions: List of transitions collected during the episode
                       (only if benchmark is False).
        - step_count: The number of steps taken in the episode
                      (only if benchmark is False).
        """
        return None, None, None

    def evaluate_models(self):
        """
        Evaluate the current model against the candidate model. The current model
        generates data until a new model outperforms it via benchmarking.
        This happens so that performance is monotonically increasing. This method
        updates the current model to be the best performing model and resets
        the candidate model for the next iteration.
        """

        # Note: Tetris is a single-player game, so only the candidate model needs to be benchmarked
        # against the previously computed max_benchmark_score.

        benchmark_candidate_score = 0
        for _ in range(self.benchmark_episode_count):
            score, _, _ = self.run_episode(model=self.candidate_model, benchmark=True)
            benchmark_candidate_score += score

        # If candidate model outperforms the best model so far, it is adopted for data generation
        if benchmark_candidate_score > self.max_benchmark_score:
            self.max_benchmark_score = benchmark_candidate_score
            self.model.load_state_dict(self.candidate_model.state_dict())

        # In either case, the candidate model is reinstantiated, resetting the
        # optimiser/scheduler but copying the weights
        self.candidate_model = A0ResNet(num_residual_blocks=19, num_actions=ACTION_SPACE)
        self.candidate_model.load_state_dict(self.model.state_dict())
