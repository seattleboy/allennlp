import os
from subprocess import run
from typing import Tuple

import torch
from overrides import overrides

from allennlp.common.file_utils import cached_path
from allennlp.common.util import JsonDict, sanitize
from allennlp.data import DatasetReader, Instance
from allennlp.models import Model
from allennlp.service.predictors.predictor import Predictor

# TODO(mattg): We should merge how this works with how the `WikiTablesAccuracy` metric works, maybe
# just removing the need for adding this stuff at all, because the parser already runs the java
# process.  This requires modifying the scala `wikitables-executor` code to also return the
# denotation when running it as a server, and updating the model to parse the output correctly, but
# that shouldn't be too hard.
DEFAULT_EXECUTOR_JAR = "https://s3-us-west-2.amazonaws.com/allennlp/misc/wikitables-executor-0.1.0.jar"
ABBREVIATIONS_FILE = "https://s3-us-west-2.amazonaws.com/allennlp/misc/wikitables-abbreviations.tsv"
GROW_FILE = "https://s3-us-west-2.amazonaws.com/allennlp/misc/wikitables-grow.grammar"
SEMPRE_DIR = 'data/'

@Predictor.register('wikitables-parser')
class WikiTablesParserPredictor(Predictor):
    """
    Wrapper for the
    :class:`~allennlp.models.encoder_decoders.wikitables_semantic_parser.WikiTablesSemanticParser`
    model.
    """

    def __init__(self, model: Model, dataset_reader: DatasetReader) -> None:
        super().__init__(model, dataset_reader)
        # Load auxiliary sempre files during startup for faster logical form execution.
        os.makedirs(SEMPRE_DIR, exist_ok=True)
        abbreviations_path = os.path.join(SEMPRE_DIR, 'abbreviations.tsv')
        if not os.path.exists(abbreviations_path):
            run(f'wget {ABBREVIATIONS_FILE}', shell=True)
            run(f'mv wikitables-abbreviations.tsv {abbreviations_path}', shell=True)

        grammar_path = os.path.join(SEMPRE_DIR, 'grow.grammar')
        if not os.path.exists(grammar_path):
            run(f'wget {GROW_FILE}', shell=True)
            run(f'mv wikitables-grow.grammar {grammar_path}', shell=True)

    @overrides
    def _json_to_instance(self, json_dict: JsonDict) -> Tuple[Instance, JsonDict]:
        """
        Expects JSON that looks like ``{"question": "...", "table": "..."}``.
        """
        question_text = json_dict["question"]
        table_text = json_dict["table"]
        cells = []
        for row_index, line in enumerate(table_text.split('\n')):
            line = line.rstrip('\n')
            if row_index == 0:
                columns = line.split('\t')
            else:
                cells.append(line.split('\t'))
        # pylint: disable=protected-access
        tokenized_question = self._dataset_reader._tokenizer.tokenize(question_text.lower())  # type: ignore
        # pylint: enable=protected-access
        table_json = {"question": tokenized_question, "columns": columns, "cells": cells}
        instance = self._dataset_reader.text_to_instance(question_text,  # type: ignore
                                                         table_json,
                                                         tokenized_question=tokenized_question)
        extra_info = {'question_tokens': tokenized_question}
        return instance, extra_info

    @overrides
    def predict_json(self, inputs: JsonDict, cuda_device: int = -1) -> JsonDict:
        with torch.no_grad():
            instance, return_dict = self._json_to_instance(inputs)
            outputs = self._model.forward_on_instance(instance, cuda_device)
            outputs['answer'] = self._execute_logical_form_on_table(outputs['logical_form'],
                                                                    inputs['table'])

            return_dict.update(outputs)
            return sanitize(return_dict)

    @staticmethod
    def _execute_logical_form_on_table(logical_form, table):
        """
        The parameters are written out to files which the jar file reads and then executes the
        logical form.
        """
        logical_form_filename = os.path.join(SEMPRE_DIR, 'logical_forms.txt')
        with open(logical_form_filename, 'w') as temp_file:
            temp_file.write(logical_form + '\n')

        table_dir = os.path.join(SEMPRE_DIR, 'tsv/')
        os.makedirs(table_dir, exist_ok=True)
        # The .tsv file extension is important here since the table string parameter is in tsv format.
        # If this file was named with suffix .csv then Sempre would interpret it as comma separated
        # and return the wrong denotation.
        table_filename = 'context.tsv'
        with open(os.path.join(table_dir, table_filename), 'w', encoding='utf-8') as temp_file:
            temp_file.write(table)

        # The id, target, and utterance are ignored, we just need to get the
        # table filename into sempre's lisp format.
        test_record = ('(example (id nt-0) (utterance none) (context (graph tables.TableKnowledgeGraph %s))'
                       '(targetValue (list (description "6"))))' % (table_filename))
        test_data_filename = os.path.join(SEMPRE_DIR, 'data.examples')
        with open(test_data_filename, 'w') as temp_file:
            temp_file.write(test_record)

        # TODO(matt): The jar that we have isn't optimal for this use case - we're using a
        # script designed for computing accuracy, and just pulling out a piece of it. Writing
        # a new entry point to the jar that's tailored for this use would be cleaner.
        command = ' '.join(['java',
                            '-jar',
                            cached_path(DEFAULT_EXECUTOR_JAR),
                            test_data_filename,
                            logical_form_filename,
                            table_dir])
        run(command, shell=True)

        denotations_file = os.path.join(SEMPRE_DIR, 'logical_forms_denotations.tsv')
        with open(denotations_file) as temp_file:
            line = temp_file.readline().split('\t')
            return line[1] if len(line) > 1 else line[0]
