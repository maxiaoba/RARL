from rllab.sampler.base import BaseSampler
import THEANO.FSPGIM_parallel_sampler as parallel_sampler
from rllab.sampler.stateful_pool import singleton_pool
import tensorflow as tf
from rllab.misc.overrides import overrides
import numpy as np
from rllab.misc import special
from rllab.misc import tensor_utils
from rllab.algos import util
import rllab.misc.logger as logger


def worker_init_tf(G):
    G.sess = tf.Session()
    G.sess.__enter__()


def worker_init_tf_vars(G):
    G.sess.run(tf.global_variables_initializer())


class RARLSampler(BaseSampler):
    def start_worker(self):
        if singleton_pool.n_parallel > 1:
            singleton_pool.run_each(worker_init_tf)
        parallel_sampler.populate_task(self.algo.env, self.algo.policy, self.algo.policy2, self.algo.regressor1, self.algo.regressor2,scope=self.algo.scope)
        if singleton_pool.n_parallel > 1:
            singleton_pool.run_each(worker_init_tf_vars)

    def shutdown_worker(self):
        parallel_sampler.terminate_task(scope=self.algo.scope)

    def obtain_samples(self, itr, player1_avg, player2_avg, policy_num, action2_limit):
        cur_params1 = self.algo.policy.get_param_values()
        cur_params2 = self.algo.policy2.get_param_values()
        cur_params3 = self.algo.regressor1.get_param_values()
        cur_params4 = self.algo.regressor2.get_param_values()
        cur_env_params = self.algo.env.get_param_values()
        paths = parallel_sampler.sample_paths(
            policy1_params=cur_params1,
            policy2_params=cur_params2,
            regressor1_params = cur_params3,
            regressor2_params = cur_params4,
            player1_avg = player1_avg,
            player2_avg = player2_avg,
            policy_num=policy_num,
            action2_limit=action2_limit,
            env_params=cur_env_params,
            max_samples=self.algo.batch_size,
            max_path_length=self.algo.max_path_length,
            scope=self.algo.scope,
        )
        if self.algo.whole_paths:
            return paths
        else:
            paths_truncated = parallel_sampler.truncate_paths(paths, self.algo.batch_size)
            return paths_truncated

    @overrides
    def process_samples(self, itr, paths, policy_num):
        baselines = []
        returns = []
        if policy_num == 1:
            if hasattr(self.algo.baseline, "predict_n"):
                all_path_baselines = self.algo.baseline.predict_n(paths)
            else:
                all_path_baselines = [self.algo.baseline.predict(path) for path in paths]

            for idx, path in enumerate(paths):
                path_baselines = np.append(all_path_baselines[idx], 0)
                deltas = path["rewards"] + \
                         self.algo.discount * path_baselines[1:] - \
                         path_baselines[:-1]
                path["advantages"] = special.discount_cumsum(
                    deltas, self.algo.discount * self.algo.gae_lambda)
                path["returns"] = special.discount_cumsum(path["rewards"], self.algo.discount)
                baselines.append(path_baselines[:-1])
                returns.append(path["returns"])

            ev = special.explained_variance_1d(
                np.concatenate(baselines),
                np.concatenate(returns)
            )

            if not self.algo.policy.recurrent:
                observations = tensor_utils.concat_tensor_list([path["observations"] for path in paths])
                actions = tensor_utils.concat_tensor_list([path["actions"] for path in paths])
                rewards = tensor_utils.concat_tensor_list([path["rewards"] for path in paths])
                returns = tensor_utils.concat_tensor_list([path["returns"] for path in paths])
                advantages = tensor_utils.concat_tensor_list([path["advantages"] for path in paths])
                env_infos = tensor_utils.concat_tensor_dict_list([path["env_infos"] for path in paths])
                agent_infos = tensor_utils.concat_tensor_dict_list([path["agent_infos"] for path in paths])

                if self.algo.center_adv:
                    advantages = util.center_advantages(advantages)

                if self.algo.positive_adv:
                    advantages = util.shift_advantages_to_positive(advantages)

                average_discounted_return = \
                    np.mean([path["returns"][0] for path in paths])

                undiscounted_returns = [sum(path["rewards"]) for path in paths]

                ent = np.mean(self.algo.policy.distribution.entropy(agent_infos))

                samples_data = dict(
                    observations=observations,
                    actions=actions,
                    rewards=rewards,
                    returns=returns,
                    advantages=advantages,
                    env_infos=env_infos,
                    agent_infos=agent_infos,
                    paths=paths,
                )
            else:
                max_path_length = max([len(path["advantages"]) for path in paths])

                # make all paths the same length (pad extra advantages with 0)
                obs = [path["observations"] for path in paths]
                obs = tensor_utils.pad_tensor_n(obs, max_path_length)

                if self.algo.center_adv:
                    raw_adv = np.concatenate([path["advantages"] for path in paths])
                    adv_mean = np.mean(raw_adv)
                    adv_std = np.std(raw_adv) + 1e-8
                    adv = [(path["advantages"] - adv_mean) / adv_std for path in paths]
                else:
                    adv = [path["advantages"] for path in paths]

                adv = np.asarray([tensor_utils.pad_tensor(a, max_path_length) for a in adv])

                actions = [path["actions"] for path in paths]
                actions = tensor_utils.pad_tensor_n(actions, max_path_length)

                rewards = [path["rewards"] for path in paths]
                rewards = tensor_utils.pad_tensor_n(rewards, max_path_length)

                returns = [path["returns"] for path in paths]
                returns = tensor_utils.pad_tensor_n(returns, max_path_length)

                agent_infos = [path["agent_infos"] for path in paths]
                agent_infos = tensor_utils.stack_tensor_dict_list(
                    [tensor_utils.pad_tensor_dict(p, max_path_length) for p in agent_infos]
                )

                env_infos = [path["env_infos"] for path in paths]
                env_infos = tensor_utils.stack_tensor_dict_list(
                    [tensor_utils.pad_tensor_dict(p, max_path_length) for p in env_infos]
                )

                valids = [np.ones_like(path["returns"]) for path in paths]
                valids = tensor_utils.pad_tensor_n(valids, max_path_length)

                average_discounted_return = \
                    np.mean([path["returns"][0] for path in paths])

                undiscounted_returns = [sum(path["rewards"]) for path in paths]

                ent = np.sum(self.algo.policy.distribution.entropy(agent_infos) * valids) / np.sum(valids)

                samples_data = dict(
                    observations=obs,
                    actions=actions,
                    advantages=adv,
                    rewards=rewards,
                    returns=returns,
                    valids=valids,
                    agent_infos=agent_infos,
                    env_infos=env_infos,
                    paths=paths,
                )

            logger.log("fitting baseline...")
            if hasattr(self.algo.baseline, 'fit_with_samples'):
                self.algo.baseline.fit_with_samples(paths, samples_data)
            else:
                self.algo.baseline.fit(paths)
            logger.log("fitted")

            logger.record_tabular('Iteration', itr)
            logger.record_tabular('AverageDiscountedReturn',
                                  average_discounted_return)
            logger.record_tabular('AverageReturn', np.mean(undiscounted_returns))
            logger.record_tabular('ExplainedVariance', ev)
            logger.record_tabular('NumTrajs', len(paths))
            logger.record_tabular('Entropy', ent)
            logger.record_tabular('Perplexity', np.exp(ent))
            logger.record_tabular('StdReturn', np.std(undiscounted_returns))
            logger.record_tabular('MaxReturn', np.max(undiscounted_returns))
            logger.record_tabular('MinReturn', np.min(undiscounted_returns))

            return samples_data
        else:
            if hasattr(self.algo.baseline2, "predict_n"):
                all_path_baselines = self.algo.baseline2.predict_n(paths)
            else:
                all_path_baselines = [self.algo.baseline2.predict(path) for path in paths]

            for idx, path in enumerate(paths):
                path_baselines = np.append(all_path_baselines[idx], 0)
                deltas = path["rewards"] + \
                         self.algo.discount * path_baselines[1:] - \
                         path_baselines[:-1]
                path["advantages"] = special.discount_cumsum(
                    deltas, self.algo.discount * self.algo.gae_lambda)
                path["returns"] = special.discount_cumsum(path["rewards"], self.algo.discount)
                baselines.append(path_baselines[:-1])
                returns.append(path["returns"])

            ev = special.explained_variance_1d(
                np.concatenate(baselines),
                np.concatenate(returns)
            )

            if not self.algo.policy.recurrent:
                observations = tensor_utils.concat_tensor_list([path["observations"] for path in paths])
                actions = tensor_utils.concat_tensor_list([path["actions"] for path in paths])
                rewards = tensor_utils.concat_tensor_list([path["rewards"] for path in paths])
                returns = tensor_utils.concat_tensor_list([path["returns"] for path in paths])
                advantages = tensor_utils.concat_tensor_list([path["advantages"] for path in paths])
                env_infos = tensor_utils.concat_tensor_dict_list([path["env_infos"] for path in paths])
                agent_infos = tensor_utils.concat_tensor_dict_list([path["agent_infos"] for path in paths])

                if self.algo.center_adv:
                    advantages = util.center_advantages(advantages)

                if self.algo.positive_adv:
                    advantages = util.shift_advantages_to_positive(advantages)

                average_discounted_return = \
                    np.mean([path["returns"][0] for path in paths])

                undiscounted_returns = [sum(path["rewards"]) for path in paths]

                ent = np.mean(self.algo.policy.distribution.entropy(agent_infos))

                samples_data = dict(
                    observations=observations,
                    actions=actions,
                    rewards=rewards,
                    returns=returns,
                    advantages=advantages,
                    env_infos=env_infos,
                    agent_infos=agent_infos,
                    paths=paths,
                )
            else:
                max_path_length = max([len(path["advantages"]) for path in paths])

                # make all paths the same length (pad extra advantages with 0)
                obs = [path["observations"] for path in paths]
                obs = tensor_utils.pad_tensor_n(obs, max_path_length)

                if self.algo.center_adv:
                    raw_adv = np.concatenate([path["advantages"] for path in paths])
                    adv_mean = np.mean(raw_adv)
                    adv_std = np.std(raw_adv) + 1e-8
                    adv = [(path["advantages"] - adv_mean) / adv_std for path in paths]
                else:
                    adv = [path["advantages"] for path in paths]

                adv = np.asarray([tensor_utils.pad_tensor(a, max_path_length) for a in adv])

                actions = [path["actions"] for path in paths]
                actions = tensor_utils.pad_tensor_n(actions, max_path_length)

                rewards = [path["rewards"] for path in paths]
                rewards = tensor_utils.pad_tensor_n(rewards, max_path_length)

                returns = [path["returns"] for path in paths]
                returns = tensor_utils.pad_tensor_n(returns, max_path_length)

                agent_infos = [path["agent_infos"] for path in paths]
                agent_infos = tensor_utils.stack_tensor_dict_list(
                    [tensor_utils.pad_tensor_dict(p, max_path_length) for p in agent_infos]
                )

                env_infos = [path["env_infos"] for path in paths]
                env_infos = tensor_utils.stack_tensor_dict_list(
                    [tensor_utils.pad_tensor_dict(p, max_path_length) for p in env_infos]
                )

                valids = [np.ones_like(path["returns"]) for path in paths]
                valids = tensor_utils.pad_tensor_n(valids, max_path_length)

                average_discounted_return = \
                    np.mean([path["returns"][0] for path in paths])

                undiscounted_returns = [sum(path["rewards"]) for path in paths]

                ent = np.sum(self.algo.policy.distribution.entropy(agent_infos) * valids) / np.sum(valids)

                samples_data = dict(
                    observations=obs,
                    actions=actions,
                    advantages=adv,
                    rewards=rewards,
                    returns=returns,
                    valids=valids,
                    agent_infos=agent_infos,
                    env_infos=env_infos,
                    paths=paths,
                )

            logger.log("fitting baseline...")
            if hasattr(self.algo.baseline2, 'fit_with_samples'):
                self.algo.baseline2.fit_with_samples(paths, samples_data)
            else:
                self.algo.baseline2.fit(paths)
            logger.log("fitted")

            logger.record_tabular('Iteration', itr)
            logger.record_tabular('AverageDiscountedReturn',
                                  average_discounted_return)
            logger.record_tabular('AverageReturn', np.mean(undiscounted_returns))
            logger.record_tabular('ExplainedVariance', ev)
            logger.record_tabular('NumTrajs', len(paths))
            logger.record_tabular('Entropy', ent)
            logger.record_tabular('Perplexity', np.exp(ent))
            logger.record_tabular('StdReturn', np.std(undiscounted_returns))
            logger.record_tabular('MaxReturn', np.max(undiscounted_returns))
            logger.record_tabular('MinReturn', np.min(undiscounted_returns))

            return samples_data
