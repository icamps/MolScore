import os
import signal
import time
import json
import logging
import numpy as np
import subprocess

import molscore.scoring_functions as scoring_functions
from molscore import utils
from molscore.utils import dash_utils
import molscore.scaffold_memory as scaffold_memory

import pandas as pd
from rdkit.Chem import AllChem as Chem

logger = logging.getLogger('molscore')
logger.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
logger.addHandler(ch)


class MolScore:

    def __init__(self, config: str):
        """
        Central manager class that, when called, takes in a list of SMILES and returns respective scores.

        :param config: File containing parameters (.json), see external documentation.
        """
        # Load in json file
        with open(config, "r") as f:
            configs = f.read().replace('\r', '').replace('\n', '').replace('\t', '')
        self.configs = json.loads(configs)

        # Initialize some attributes
        self.step = 0
        self.init_time = time.time()
        self.results_df = None
        self.batch_df = None
        self.exists_df = None
        self.main_df = None
        self.dash_monitor = None
        self.logged_parameters = {}

        # Setup dash_utils monitor
        if self.configs['dash_monitor']['run']:
            self.dash_monitor = True
            if self.configs['dash_monitor']['pdb_path'] is not None:
                self.dash_monitor_path = dash_utils.dash_monitor3D_path
            else:
                self.dash_monitor_path = dash_utils.dash_monitor_path

        # Setup save directory
        self.run_name = "_".join([time.strftime("%Y_%m_%d", time.localtime()),
                                  self.configs['logging']['model']['name'].replace(" ", "_"),
                                  self.configs['logging']['task']['name'].replace(" ", "_")])
        self.save_dir = os.path.join(os.path.abspath(self.configs['output_dir']), self.run_name)
        # Check to see if we're loading from previous results
        if self.configs['load_from_previous']:
            assert os.path.exists(self.configs['previous_dir']), "Previous directory does not exist"
            self.save_dir = self.configs['previous_dir']
        else:
            if os.path.exists(self.save_dir):
                logger.warning("Found existing directory, appending current time to distinguish")
                self.save_dir = self.save_dir + time.strftime("_%H_%M_%S", time.localtime())
            os.makedirs(self.save_dir)
            os.makedirs(os.path.join(self.save_dir, 'iterations'))

        # Setup log file
        self.fh = logging.FileHandler(os.path.join(self.save_dir, 'log.txt'))
        self.fh.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        self.fh.setFormatter(formatter)
        logger.addHandler(self.fh)

        # Write out config
        with open(os.path.join(self.save_dir, f"{self.run_name}_config.json"), "w") as config_f:
            json.dump(self.configs, config_f)

        # Setup scoring functions
        self.scoring_functions = []
        for fconfig in self.configs['scoring_functions']:
            if fconfig['run']:
                for fclass in scoring_functions.all_scoring_functions:
                    if fclass.__name__ == fconfig['name']:
                        self.scoring_functions.append(fclass(**fconfig['parameters']))
                if all([fclass.__name__ != fconfig['name'] for fclass in scoring_functions.all_scoring_functions]):
                    logger.warning(f'Not found associated scoring function for {fconfig["name"]}')
            else:
                pass
        assert len(self.scoring_functions) > 0, "No scoring functions assigned"

        # Setup modifiers
        self.modifier_functions = utils.all_score_modifiers

        # Setup mpo methods
        for func in utils.all_score_methods:
            if self.configs['scoring']['method'] == func.__name__:
                self.mpo_method = func
        assert any([self.configs['scoring']['method'] == func.__name__ for func in utils.all_score_methods])

        # Setup diversity filter (adapted from Blaschke et al.)
        self.diversity_filter = None
        if self.configs['diversity_filter']['run']:
            for filt in scaffold_memory.all_scaffold_filters:
                if self.configs['diversity_filter']['name'] == filt.__name__:
                    self.diversity_filter = filt(**self.configs['diversity_filter']['parameters'])
                    self.log_parameters({'diversity_filter': self.configs['diversity_filter']['name']})
            if all([filt.__name__ != self.configs['diversity_filter']['name']
                    for filt in scaffold_memory.all_scaffold_filters]):
                logger.warning(f'Not found associated diversity filter for {self.configs["diversity_filter"]["name"]}')

        # Load from previous
        if self.configs['load_from_previous']:
            logger.info('Loading scores.csv from previous run')
            self.main_df = pd.read_csv(os.path.join(self.save_dir, 'scores.csv'),
                                       index_col=0, dtype={'Unnamed: 0': 'int64', 'valid': object, 'unique': object})
            logger.debug(self.main_df.head())
            # Update step
            self.step = max(self.main_df['step'])
            # Update time
            self.init_time = time.time() - self.main_df['absolute_time'].iloc[-1]
            # Update max min
            self.update_maxmin(df=self.main_df)

        logger.info('molscore initiated')

    def parse_smiles(self, smiles: list, step: int):
        """
        Create batch_df object from initial list of SMILES and calculate validity and
        intra-batch uniqueness

        :param smiles: List of smiles taken from generative model
        :param step: current generative model step
        """
        # Initialize df for batch
        self.batch_df = pd.DataFrame(index=range(len(smiles)))

        # Parse smiles
        parsed_smiles = []
        valid = []
        batch_idx = []
        for i, smi in enumerate(smiles):
            try:
                can_smi = Chem.MolToSmiles(Chem.MolFromSmiles(smi))
                parsed_smiles.append(can_smi)
                valid.append('true')
            except:
                try:
                    mol = Chem.MolFromSmiles(smi)
                    Chem.SanitizeMol(mol)  # Try to catch invalid molecules and sanitize
                    can_smi = Chem.MolToSmiles(mol)
                    parsed_smiles.append(can_smi)
                    valid.append('sanitized')
                except:
                    parsed_smiles.append(smi)
                    valid.append('false')
            batch_idx.append(i)

        self.batch_df['model'] = self.configs['logging']['model']['name'].replace(" ", "_")
        self.batch_df['task'] = self.configs['logging']['task']['name'].replace(" ", "_")
        self.batch_df['step'] = step
        self.batch_df['batch_idx'] = batch_idx
        self.batch_df['absolute_time'] = time.time() - self.init_time
        self.batch_df['smiles'] = parsed_smiles
        self.batch_df['valid'] = valid

        # Check for duplicates
        duplicated = self.batch_df.smiles.duplicated().tolist()
        unique = [str(not b).lower() for b in duplicated]  # Reverse true/false i.e. unique as oppose to duplicated
        self.batch_df['unique'] = unique

        # Count previous occurrences
        occurrences = [self.batch_df.smiles[:i][self.batch_df.smiles == self.batch_df.smiles[i]].count() for i in
                       self.batch_df.index]
        self.batch_df['occurrences'] = occurrences

        number_invalid = len(self.batch_df.loc[self.batch_df.valid == 'false', :])
        logger.debug(f'    Invalid molecules: {number_invalid}')
        return self

    def check_uniqueness(self):
        """
        Check batch_df smiles against main_df of any previously sampled smiles
        """

        # Pull duplicated smiles from the main df
        self.exists_df = self.main_df[self.main_df.smiles.isin(self.batch_df.smiles.tolist())]

        # Update unique and occurrence columns
        if len(self.exists_df) > 1:
            for smi in self.batch_df.smiles:
                if self.exists_df[self.exists_df.smiles == smi].any()[0]:
                    self.batch_df.loc[self.batch_df.smiles == smi, 'unique'] = 'false'
                    self.batch_df.loc[self.batch_df.smiles == smi, 'occurrences'] += self.exists_df.smiles[
                        self.exists_df.smiles == smi].count()
        return self

    def run_scoring_functions(self, smiles: list, file_names: list):
        """
        Calculate respective scoring function scores for a list of unique smiles
         (with file names for logging if necessary).

        :param smiles: A list of valid smiles, preferably without duplicated or known scores
        :param file_names: A corresponding list of file prefixes for tracking - format={step}_{batch_idx}
        :return: self.results (a list of dictionaries with smiles and resulting scores)
        """
        for function in self.scoring_functions:
            results = function(smiles=smiles, directory=self.save_dir, file_names=file_names)
            results_df = pd.DataFrame(results)

            if self.results_df is None:
                # If this is the first scoring function run in the list, copy
                self.results_df = results_df
            else:
                self.results_df = self.results_df.merge(results_df, on='smiles', how='outer', sort=False)
        return self

    def first_update(self):
        """
        Append calculated scoring function values to batch dataframe. Only used for the first step/batch.
        """
        logger.debug('    Merging results to batch df')
        self.batch_df = self.batch_df.merge(self.results_df, on='smiles', how='left', sort=False)
        self.batch_df.fillna(0.0, inplace=True)
        return self

    def concurrent_update(self):
        """
        Append calculated scoring function values to batch dataframe while looking up duplicated entries to avoid
        re-calculating.

        :return:
        """

        # Grab data for pre-existing smiles
        if len(self.exists_df) > 1:
            self.exists_df = self.exists_df.drop_duplicates(subset='smiles')
            self.exists_df = self.exists_df.loc[:, self.results_df.columns]
            # Check no duplicated values in exists and results df
            dup_idx = self.exists_df.loc[self.exists_df.smiles.isin(self.results_df.smiles), :].index.tolist()
            if len(dup_idx) > 0:
                self.exists_df.drop(index=dup_idx, inplace=True)
            # Append to results, assuming no duplicates in results_df...
            self.results_df = self.results_df.append(self.exists_df, ignore_index=True, sort=False)
            self.results_df = self.results_df.drop_duplicates(subset='smiles')

        # Merge with batch_df
        logger.debug('    Merging results to batch df')
        self.batch_df = self.batch_df.merge(self.results_df, on='smiles', how='left', sort=False)
        self.batch_df.fillna(0.0, inplace=True)
        return self

    def update_maxmin(self, df):
        """
        This function keeps track of maximum and minimum values seen per metric for normalization purposes.

        :return:
        """
        for metric in self.configs['scoring']['metrics']:
            if metric['name'] in df.columns:
                df_max = df.loc[:, metric['name']].max()
                df_min = df.loc[:, metric['name']].min()

                if 'max' not in metric['parameters'].keys():
                    metric['parameters'].update({'max': df_max})
                    logger.debug(f"    Updated max to {df_max}")
                elif df_max > metric['parameters']['max']:
                    metric['parameters'].update({'max': df_max})
                    logger.debug(f"    Updated max to {df_max}")
                else:
                    pass

                if 'min' not in metric['parameters'].keys():
                    metric['parameters'].update({'min': df_min})
                    logger.debug(f"    Updated min to {df_min}")
                elif df_min < metric['parameters']['min']:
                    metric['parameters'].update({'min': df_min})
                    logger.debug(f"    Updated min to {df_min}")
                else:
                    pass
        return self

    def compute_score(self, df):
        """
        Compute the final score i.e. combination of which metrics according to which method.
        """

        mpo_columns = {"names": [], "weights": []}
        # Iterate through specified metrics and apply modifier
        for metric in self.configs['scoring']['metrics']:
            mod_name = f"{metric['modifier']}_{metric['name']}"
            mpo_columns["names"].append(mod_name)
            mpo_columns["weights"].append(metric['weight'])

            for mod in self.modifier_functions:
                if metric['modifier'] == mod.__name__:
                    modifier = mod

            # Check the modifier function exists, and the metric can be found in the dataframe
            assert any([metric['modifier'] == mod.__name__ for mod in self.modifier_functions]), \
                "Score modifier not found"
            assert metric['name'] in df.columns, "Specified metric not found in dataframe"

            df[mod_name] = df.loc[:, metric['name']].apply(
                lambda x: modifier(x, **metric['parameters'])
            )

        # Double check we have no NaN or 0 values (necessary for geometric mean) for mpo columns
        df.loc[:, mpo_columns['names']] = df.loc[:, mpo_columns['names']].fillna(1e-6)
        df.loc[:, mpo_columns['names']] = df.loc[:, mpo_columns['names']].apply(
            lambda x: [1e-6 if y < 1e-6 else y for y in x]
        )

        # Compute final score (df not used by mpo_method except for Pareto pair [not implemented])
        df[self.configs['scoring']['method']] = df.loc[:, mpo_columns['names']].apply(
            lambda x: self.mpo_method(X=x, W=mpo_columns['weights'], df=self.main_df), axis=1
        )

        # Run through diversity filter if applicable
        if self.diversity_filter is not None:
            scores_dict = {"total_score": np.asarray(df[self.configs['scoring']['method']].tolist(),
                                                     dtype=np.float32),
                           "step": [self.step] * len(df)}
            filtered_scores = self.diversity_filter.score(smiles=df['smiles'].tolist(),
                                                          scores_dict=scores_dict)
            df["passes_diversity_filter"] = ['true' if a == b else 'false'
                                             for b, a in
                                             zip(df[self.configs['scoring']['method']],
                                                 filtered_scores)]
            df[f"filtered_{self.configs['scoring']['method']}"] = filtered_scores
            df.fillna(1e-6)

        return df

    def log_parameters(self, parameters: dict):
        self.logged_parameters.update(parameters)
        return self

    def write_scores(self):
        """
        Function to write final dataframe to file.

        :return:
        """
        if len(self.logged_parameters) > 0:
            temp = self.main_df.copy()
            for p, v in self.logged_parameters.items():
                temp[p] = [v]*len(temp)
            temp.to_csv(os.path.join(self.save_dir, 'scores.csv'))  # save main csv
        else:
            self.main_df.to_csv(os.path.join(self.save_dir, 'scores.csv'))  # save main csv

        if self.diversity_filter is not None:
            self.diversity_filter.savetocsv(os.path.join(self.save_dir, 'scaffold_memory.csv'))

        self.fh.close()

        return self

    def run_dash_monitor(self):
        """
        Run Dash Monitor.
        """
        # Start dash_utils monitor (Killed in write scores method)
        cmd = ['python', self.dash_monitor_path, self.save_dir]
        if self.configs['dash_monitor']['pdb_path'] is not None:
            cmd += [self.configs['dash_monitor']['pdb_path']]
        self.dash_monitor = subprocess.Popen(cmd,
                                             preexec_fn=os.setsid)
        return self

    def kill_dash_monitor(self):
        """
        Kill dash_utils monitor
        """
        if self.dash_monitor is None:
            logger.info('No dash monitor to kill')
            return self
        else:
            os.killpg(os.getpgid(self.dash_monitor.pid), signal.SIGTERM)
            _, _ = self.dash_monitor.communicate()
        return self

    def __call__(self, smiles: list, step: int = None, flt: bool = False, recalculate: bool = False,
                 score_only: bool = False):
        """
        Calling MolScore will result in the primary function of scoring smiles and logging data in
         an automated fashion.

        :param smiles: A list of smiles for scoring.
        :param step: Step of generative model for logging, and indexing. This could equally be iterations/epochs etc.
        :param flt: Whether to return a list of floats (default False i.e. return np.array of type np.float32)
        :param recalculate: Whether to recalculate scores for duplicated values,
         in case scoring function may be somewhat stochastic.
          (default False i.e. use existing scores for duplicated molecules)
        :param score_only: Whether to log molecule data or simply score and return
        :return: Scores (either float list or np.array)
        """
        if score_only:
            batch_start = time.time()
            if step is not None:
                self.step = step
            else:
                self.step += 1
            logger.info(f'   Received: {len(smiles)}')
            logger.info(f'   Scoring: {len(smiles)} SMILES')
            file_names = [f'{step}_{i}' for i, smi in enumerate(smiles)]
            self.run_scoring_functions(smiles=smiles, file_names=file_names)
            logger.info(f'    Returned score for {len(self.results_df)} SMILES')
            logger.info(f'    Scoring elapsed time: {time.time() - batch_start:.02f}s')
            logger.info(f'    Computing score')
            self.update_maxmin(self.results_df)
            self.results_df = self.compute_score(self.results_df)
            # Fetch score
            if self.diversity_filter is not None:
                scores = self.results_df.loc[:, f"filtered_{self.configs['scoring']['method']}"].tolist()
            else:
                scores = self.results_df.loc[:, self.configs['scoring']['method']].tolist()
            if not flt:
                scores = np.array(scores, dtype=np.float32)
            logger.info(f'    Returning {len(scores)} scores')
            logger.info(f'    MolScore elapsed time: {time.time() - batch_start:.02f}s')

            # Clean up class
            self.batch_df = None
            self.exists_df = None
            self.results_df = None
            return scores

        else:
            # Set some values
            batch_start = time.time()
            if step is not None:
                self.step = step
            else:
                self.step += 1
            logger.info(f'STEP {self.step}')
            logger.info(f'    Received: {len(smiles)} SMILES')

            # Parse smiles and initiate batch df
            self.parse_smiles(smiles=smiles, step=self.step)
            logger.info(f'    Pre-processed: {len(self.batch_df)} SMILES')

            # If a main df exists check if some molecules have already been sampled
            if isinstance(self.main_df, pd.core.frame.DataFrame):
                self.check_uniqueness()
                logger.info(f'    Uniqueness updated: {len(self.batch_df)} SMILES')
                logger.info(f'    Duplicates found: {self.batch_df.unique[self.batch_df.unique == "false"].count()} SMILES')

            # Subset only unique and valid smiles
            if recalculate:
                smiles_to_process = self.batch_df.loc[self.batch_df.valid.isin(['true', 'sanitized']),
                                                      'smiles'].tolist()
                smiles_to_process_index = self.batch_df.loc[self.batch_df.valid.isin(['true', 'sanitized']),
                                                            'batch_idx'].tolist()
            else:
                smiles_to_process = self.batch_df.loc[(self.batch_df.valid.isin(['true', 'sanitized'])) &
                                                      (self.batch_df.unique == 'true'), 'smiles'].tolist()
                smiles_to_process_index = self.batch_df.loc[(self.batch_df.valid.isin(['true', 'sanitized'])) &
                                                            (self.batch_df.unique == 'true'), 'batch_idx'].tolist()
            if len(smiles_to_process) == 0:
                # If no smiles to process then instead submit all (scoring function should handle invalid)
                logger.info(f'    No smiles to score so submitting first 10 SMILES')
                smiles_to_process = self.batch_df.loc[:9, 'smiles'].tolist()
                smiles_to_process_index = self.batch_df.loc[:9, 'batch_idx'].tolist()

            assert len(smiles_to_process) == len(smiles_to_process_index)
            file_names = [f'{self.step}_{i}' for i in smiles_to_process_index]
            logger.info(f'    Scoring: {len(smiles_to_process)} SMILES')

            # Run scoring function
            scoring_start = time.time()
            self.run_scoring_functions(smiles=smiles_to_process, file_names=file_names)
            logger.info(f'    Returned score for {len(self.results_df)} SMILES')
            logger.info(f'    Scoring elapsed time: {time.time() - scoring_start:.02f}s')

            # Append scoring results
            if isinstance(self.main_df, pd.core.frame.DataFrame) and not recalculate:
                self.concurrent_update()
            else:
                self.first_update()
            logger.info(f'    Scores updated: {len(self.batch_df)} SMILES')

            # Compute average / score
            self.update_maxmin(df=self.batch_df)
            self.batch_df = self.compute_score(df=self.batch_df)
            logger.info(f'    Aggregate score calculated: {len(self.batch_df)} SMILES')

            # Add information of scoring time
            self.batch_df['score_time'] = time.time() - scoring_start

            # Append batch df to main df if it exists, else initialise it.
            if isinstance(self.main_df, pd.core.frame.DataFrame):
                # update indexing based on most recent index
                self.batch_df.index = self.batch_df.index + self.main_df.index[-1] + 1
                self.main_df = self.main_df.append(self.batch_df)
            else:
                self.main_df = self.batch_df.copy()

            # Write out csv log for each iteration
            self.batch_df.to_csv(os.path.join(self.save_dir, 'iterations', f'{self.step:06d}_scores.csv'))

            # Start dash_utils monitor to track iteration files once first one is written!
            if self.dash_monitor is True:
                self.run_dash_monitor()

            # Fetch score
            if self.diversity_filter is not None:
                scores = self.batch_df.loc[:, f"filtered_{self.configs['scoring']['method']}"].tolist()
            else:
                scores = self.batch_df.loc[:, self.configs['scoring']['method']].tolist()
            if not flt:
                scores = np.array(scores, dtype=np.float32)
            logger.info(f'    Returning {len(scores)} scores')
            logger.info(f'    MolScore elapsed time: {time.time() - batch_start:.02f}s')

            # Clean up class
            self.batch_df = None
            self.exists_df = None
            self.results_df = None

            return scores
