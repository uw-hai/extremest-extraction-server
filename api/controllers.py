
from random import sample, shuffle, random
import pickle
import sys
from app import app
from ml.extractors.cnn_core.test import test_cnn
from ml.extractors.cnn_core.computeScores import computeScores

from util import write_model_to_file, retrain, get_unlabeled_examples_from_tackbp, split_examples
from crowdjs_util import make_labeling_crowdjs_task, make_recall_crowdjs_task, make_precision_crowdjs_task
import urllib2
from schema.job import Job
from math import floor, ceil, sqrt, log
import numpy as np


def test_controller(task_information, task_category_id):


    some_examples_to_test_with = []
    expected_labels = []
    with open('data/test_data/general_events_death', 'r') as f:
        for example in f:
            some_examples_to_test_with.append(example)
            expected_labels.append(-1)
            
    if task_category_id == 2:
        task = make_labeling_crowdjs_task(some_examples_to_test_with,
                                          expected_labels,
                                          task_information)
        return 2, task, len(some_examples_to_test_with) * app.config['CONTROLLER_LABELS_PER_QUESTION'], 0

    elif task_category_id == 0:
        task = make_recall_crowdjs_task(task_information)
        return 0, task, app.config['CONTROLLER_GENERATE_BATCH_SIZE'], 0

    elif task_category_id == 1:
        task = make_precision_crowdjs_task(some_examples_to_test_with,
                                        task_information)
        return 1, task, len(some_examples_to_test_with), 0

    
#Alternate back and forth between precision and recall categories.
#Then, use the other half of the budget and
#select a bunch of examples from TACKBP corpus to label.
def round_robin_controller(task_ids, task_categories, training_examples,
                      training_labels, task_information,
                      costSoFar, budget, job_id):


    print "Round-Robin Controller activated."
    sys.stdout.flush()
        
    if len(task_categories) % 3 == 2:
        next_category = app.config['EXAMPLE_CATEGORIES'][2]
        
        selected_examples, expected_labels = get_unlabeled_examples_from_tackbp(
            task_ids, task_categories,
            training_examples, training_labels,
            task_information, costSoFar,
            budget, job_id)
        
        task = make_labeling_crowdjs_task(selected_examples,
                                          expected_labels,
                                          task_information)
 
        return next_category['id'], task, len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'], len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'] * next_category['price']

    if len(task_categories) % 3 == 0:
        print "choosing the RECALL category"
        sys.stdout.flush()
    
        next_category = app.config['EXAMPLE_CATEGORIES'][0]
        
        task = make_recall_crowdjs_task(task_information)
                                        
        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE']
        return next_category['id'], task, num_hits, num_hits * next_category['price']

    if len(task_categories) % 3 == 1:

        last_batch = training_examples[-1]
        next_category = app.config['EXAMPLE_CATEGORIES'][1]

        task = make_precision_crowdjs_task(last_batch, task_information)

        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE'] * app.config[
            'CONTROLLER_NUM_MODIFY_TASKS_PER_SENTENCE']
        
        return next_category['id'], task, num_hits, num_hits*next_category['price']


#Pick the action corresponding to the distribution that the extractor performs
#most poorly on.
def uncertainty_sampling_controller(task_ids, task_categories,
                                    training_examples,
                                     training_labels, task_information,
                                     costSoFar, budget, job_id):



    print "Uncertainty Sampling Controller activated."
    sys.stdout.flush()

    if len(task_categories) < 3:
        return  round_robin_controller(
            task_ids,task_categories, training_examples,
            training_labels, task_information,
            costSoFar, budget, job_id)

    categories_to_examples = {}
    for i, task_category in zip(range(len(task_categories)), task_categories):

        #This check is because some data in the database is inconsistent
        if isinstance(task_category, dict):
            task_category_id = task_category['id']
        else:
            task_category_id = task_category

        if not task_category_id in categories_to_examples:
            categories_to_examples[task_category_id] = []

        categories_to_examples[task_category_id].append(task_ids[i])

    #For every kind of action, check to see how well the extractor can
    #predict it
    worst_task_category_id = []
    worst_fscore = 1.0
    for target_task_category_id in categories_to_examples.keys():

        training_positive_examples = []
        training_negative_examples = []
        validation_positive_examples = []
        validation_negative_examples = []
        validation_all_examples = []
        validation_all_labels = []
        
        for task_category_id  in categories_to_examples.keys():
            matching_task_ids = categories_to_examples[task_category_id]
            pos_examples, neg_examples = split_examples(
                matching_task_ids,
                [task_category_id for i in matching_task_ids],
                ['all'])
            if not task_category_id == target_task_category_id:
                training_positive_examples += pos_examples
                training_negative_examples += neg_examples
            else:
                shuffle(pos_examples)
                shuffle(neg_examples)

                size_of_validation_positive_examples = int(
                    ceil(0.2 * len(pos_examples)))
                size_of_validation_negative_examples = int(
                    ceil(0.2 * len(neg_examples)))
                
                validation_positive_examples += pos_examples[
                    0:size_of_validation_positive_examples]
                validation_negative_examples += neg_examples[
                    0:size_of_validation_negative_examples]

                training_positive_examples += pos_examples[
                    size_of_validation_positive_examples:]
                training_negative_examples += neg_examples[
                    size_of_validation_negative_examples:]

        validation_all_examples = (validation_positive_examples +
                                   validation_negative_examples)
        validation_all_labels = (
            [1 for e in range(len(validation_positive_examples))]+
            [0 for e in range(len(validation_negative_examples))])

        print "RETRAINING TO FIGURE OUT WHAT ACTION TO DO NEXT"
        print len(training_positive_examples)
        print len(training_negative_examples)
        print len(validation_all_examples)
        
        retrain(job_id, ['all'],
                training_positive_examples = training_positive_examples,
                training_negative_examples = training_negative_examples)

        job = Job.objects.get(id = job_id)
        vocabulary = pickle.loads(job.vocabulary)
        predicted_labels = test_cnn(
            validation_all_examples,
            validation_all_labels,
            write_model_to_file(job_id),
            vocabulary)

        precision, recall, f1 = computeScores(predicted_labels,
                                              validation_all_labels)

        print "Action:"
        print target_task_category_id
        print "Scores:"
        print precision, recall, f1
        sys.stdout.flush()

        if f1 < worst_fscore:
            worst_fscore =  f1
            worst_task_category_id = [target_task_category_id]
        elif f1 == worst_fscore:
            worst_task_category_id.append(target_task_category_id)
            
    print "Worst F Score"
    print worst_fscore
    sys.stdout.flush()

    worst_task_category_id = sample(worst_task_category_id, 1)[0]
    
    if worst_task_category_id == 2:
        print "choosing the LABEL category"
        sys.stdout.flush()

        next_category = app.config['EXAMPLE_CATEGORIES'][2]
        
        (selected_examples,
         expected_labels) = get_unlabeled_examples_from_tackbp(
             task_ids, task_categories, training_examples,
             training_labels, task_information, costSoFar,
             budget, job_id)
        
        task = make_labeling_crowdjs_task(selected_examples,
                                          expected_labels,
                                          task_information)
        
        return 2, task, len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'], len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'] * next_category['price']

    elif worst_task_category_id == 0:
        print "choosing the RECALL category"
        sys.stdout.flush()
        
        next_category = app.config['EXAMPLE_CATEGORIES'][0]
        
        task = make_recall_crowdjs_task(task_information)
        
        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE']
        return 0, task, num_hits, num_hits * next_category['price']
    
    elif worst_task_category_id == 1:
        print "choosing the PRECISION category"
        sys.stdout.flush()

        next_category = app.config['EXAMPLE_CATEGORIES'][1]

        #positive_examples = []

        generate_task_ids = categories_to_examples[0]
        positive_examples, negative_examples = split_examples(
                generate_task_ids,
                [0 for i in generate_task_ids],
                ['all'])
        #for training_example_set, training_label_set in zip(
        #        training_examples, training_labels):
        #    for training_example, training_label in zip(
        #            training_example_set, training_label_set):
        #        if training_label == 1:
        #            positive_examples.append(training_example)

        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE'] * app.config[
            'CONTROLLER_NUM_MODIFY_TASKS_PER_SENTENCE']

        selected_positive_examples = sample(positive_examples, num_hits)
        
        
        task = make_precision_crowdjs_task(selected_positive_examples,
                                           task_information)
        
        return 1, task, num_hits, num_hits * next_category['price']



#Pick the action corresponding to the distribution that the extractor performs
#most poorly on.
def impact_sampling_controller(task_ids, task_categories,
                               training_examples,
                               training_labels, task_information,
                               costSoFar, budget, job_id):
    


    print "Impact Sampling Controller activated."
    sys.stdout.flush()

    if len(task_categories) < 4:
        return  round_robin_controller(
            task_ids,task_categories, training_examples,
            training_labels, task_information,
            costSoFar, budget, job_id)


    #First update the statistics about metric improvements from the last 
    #action taken

    last_task_id = task_ids[-1]
    last_task_category = task_categories[-1]

    categories_to_examples = {}
    for i, task_category in zip(range(len(task_categories)-1), 
                                task_categories[0:-1]):

        #This check is because some data in the database is inconsistent
        if isinstance(task_category, dict):
            task_category_id = task_category['id']
        else:
            task_category_id = task_category

        if not task_category_id in categories_to_examples:
            categories_to_examples[task_category_id] = []

        categories_to_examples[task_category_id].append(task_ids[i])

    #For every kind of action, check to see how well the extractor can
    #predict it

    #Take the examples from the GENERATE category and use them to compute
    #recall.
    #Take the examples from the LABEL category and use them to compute 
    #precision.

    training_positive_examples = []
    training_negative_examples = []
    validation_recall_examples = []
    validation_recall_labels = []
    validation_precision_examples = []
    validation_precision_labels = []

    recall_measuring_task_cat_ids = [0]
    precision_measuring_task_cat_ids = [2]
    other_task_cat_ids = [1]

    for recall_measuring_task_cat_id in recall_measuring_task_cat_ids:
        recall_task_ids = categories_to_examples[
            recall_measuring_task_cat_id]
        
        recall_examples, placeholder = split_examples(
            recall_task_ids,
            [recall_measuring_task_cat_id for i in recall_task_ids],
            ['all'])
        
        if len(placeholder) > 0:
            raise Exception
            
        shuffle(recall_examples)

        size_of_validation_recall_examples = int(
            ceil(0.2 * len(recall_examples)))

        validation_recall_examples += recall_examples[
            0:size_of_validation_recall_examples]

        validation_recall_labels += [1 for e in range(
            size_of_validation_recall_examples)]

        training_positive_examples += recall_examples[
            size_of_validation_recall_examples:]
    

        print "ADDING RECALL EXAMPLES"
        print len(training_positive_examples)
        print len(training_negative_examples)
        sys.stdout.flush()

    for precision_measuring_task_cat_id in precision_measuring_task_cat_ids:
        precision_task_ids = categories_to_examples[
            precision_measuring_task_cat_id]

        pos_examples, neg_examples = split_examples(
            precision_task_ids,
            [precision_measuring_task_cat_id for i in precision_task_ids],
            ['all'])
    
        if len(placeholder) > 0:
            raise Exception
                        
        
        shuffled_indices = np.random.permutation(
            np.arange(len(pos_examples) + len(neg_examples)))

        size_of_validation_precision_examples = int(
            ceil(0.2 * len(shuffled_indices)))

        for index in shuffled_indices[0:size_of_validation_precision_examples]:
            if index < len(pos_examples):
                validation_precision_examples.append(pos_examples[index])
                validation_precision_labels.append(1)
            else:
                real_index = index - len(pos_examples)
                validation_precision_examples.append(neg_examples[real_index])
                validation_precision_labels.append(0)

        for index in shuffled_indices[size_of_validation_precision_examples:]:
            if index < len(pos_examples):
                training_positive_examples.append(pos_examples[index])
            else:
                real_index = index - len(pos_examples)
                training_negative_examples.append(neg_examples[real_index])

        print "ADDING PRECISION EXAMPLES"
        print len(training_positive_examples)
        print len(training_negative_examples)
        sys.stdout.flush()

        

    for other_task_cat_id in other_task_cat_ids:
       other_task_ids = categories_to_examples[other_task_cat_id]
       
       pos_examples, neg_examples = split_examples(
           other_task_ids,
           [other_task_cat_id for i in other_task_ids],
           ['all'])


       training_positive_examples += pos_examples
       training_negative_examples += neg_examples


       print "ADDING ALL OTHER EXAMPLES"
       print len(training_positive_examples)
       print len(training_negative_examples)
       sys.stdout.flush()
       
               
    print "RETRAINING TO FIGURE OUT WHAT ACTION TO DO NEXT"
    print len(training_positive_examples)
    print len(training_negative_examples)
    sys.stdout.flush()
    
    retrain(job_id, ['all'],
            training_positive_examples = training_positive_examples,
            training_negative_examples = training_negative_examples)
    
    job = Job.objects.get(id = job_id)
    vocabulary = pickle.loads(job.vocabulary)


    predicted_labels = test_cnn(
        validation_recall_examples + validation_precision_examples,
        validation_recall_labels + validation_precision_labels,
        write_model_to_file(job_id),
        vocabulary)
    
    predicted_labels_for_recall_examples = predicted_labels[
        0:len(validation_recall_examples)]
    predicted_labels_for_precision_examples = predicted_labels[
        len(validation_recall_examples):]

    #compute scores separately for precision and recall
    _, recall, _ = computeScores(
        predicted_labels_for_recall_examples,
        validation_recall_labels)

    
    precision, _, _ = computeScores(
        predicted_labels_for_precision_examples,
        validation_precision_labels)

    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    print recall
    print predicted_labels_for_recall_examples
    print validation_recall_labels
    print precision
    print predicted_labels_for_precision_examples
    print validation_precision_labels
    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    sys.stdout.flush()

    if (precision + recall) == 0:
        f1 = 0.0
    else:
        f1 = 2.0 * (precision * recall) / (precision + recall)
    
    ## Add in the extra data and compute the effect

    print "ADDING BACK IN EXTRA DATA"
    print last_task_id
    print last_task_category
    sys.stdout.flush()

    pos_examples, neg_examples = split_examples(
        [last_task_id], [last_task_category], ['all'])
    

    training_positive_examples += pos_examples
    training_negative_examples += neg_examples

    
    retrain(job_id, ['all'],
            training_positive_examples = training_positive_examples,
            training_negative_examples = training_negative_examples)
    
    job = Job.objects.get(id = job_id)
    vocabulary = pickle.loads(job.vocabulary)


    predicted_labels = test_cnn(
        validation_recall_examples + validation_precision_examples,
        validation_recall_labels + validation_precision_labels,
        write_model_to_file(job_id),
        vocabulary)
    
    predicted_labels_for_recall_examples = predicted_labels[
        0:len(validation_recall_examples)]
    predicted_labels_for_precision_examples = predicted_labels[
        len(validation_recall_examples):]

    #compute scores separately for precision and recall
    _, new_recall, _ = computeScores(
        predicted_labels_for_recall_examples,
        validation_recall_labels)

    new_precision, _, _ = computeScores(
        predicted_labels_for_precision_examples,
        validation_precision_labels)


    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    print new_recall
    print predicted_labels_for_recall_examples
    print validation_recall_labels
    print new_precision
    print predicted_labels_for_precision_examples
    print validation_precision_labels
    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    sys.stdout.flush()

    if (new_precision + new_recall) == 0:
        new_f1 = 0.0
    else:
        new_f1 = (2.0 * (new_precision * new_recall) / 
                  (new_precision + new_recall))


    change_in_f1 = new_f1 - f1


    current_control_data = pickle.loads(job.control_data)

    current_control_data[last_task_category].append(change_in_f1)
            
    job.control_data = pickle.dumps(current_control_data)
    job.save()

    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    print current_control_data
    print "------------------------------------------"
    print "------------------------------------------"
    print "------------------------------------------"
    sys.stdout.flush()


    if len(task_categories) < 6:
        return  round_robin_controller(
            task_ids,task_categories, training_examples,
            training_labels, task_information,
            costSoFar, budget, job_id)


    #Add an exploration term 

    best_task_category = []
    best_change = float('-inf')
    num_actions_taken_so_far = 0.0
    for task_category in current_control_data.keys():
        num_actions_taken_so_far += len(current_control_data[task_category])

    for task_category in current_control_data.keys():
        average_change = np.mean(current_control_data[task_category])
        exploration_term =  sqrt(
            2.0*log(num_actions_taken_so_far) / 
            len(current_control_data[task_category]) )
        ucb_value = average_change + exploration_term

        print "------------------------------------------"
        print "------------------------------------------"
        print "------------------------------------------"
        print "Value of action %d" % task_category
        print current_control_data[task_category]
        print average_change
        print exploration_term
        print ucb_value
        print "------------------------------------------"
        print "------------------------------------------"
        print "------------------------------------------"
        sys.stdout.flush()

        if ucb_value > best_change:
            best_task_category = [task_category]
            best_change = ucb_value
        elif ucb_value == best_change:
            best_task_category.append(task_category)
    

    #epsilon = 1.0 / num_actions_taken_so_far
    
    #if random() < epsilon:
    #    other_choices = [0,1,2]
    #    for item in best_task_category:
    #        other_choices.remove(item)
    #    best_task_category = sample(other_choices, 1)[0]
    #else:
    best_task_category = sample(best_task_category,1)[0]

    if best_task_category == 2:
        print "choosing the LABEL category"
        sys.stdout.flush()

        next_category = app.config['EXAMPLE_CATEGORIES'][2]
        
        (selected_examples,
         expected_labels) = get_unlabeled_examples_from_tackbp(
             task_ids, task_categories, training_examples,
             training_labels, task_information, costSoFar,
             budget, job_id)
        
        task = make_labeling_crowdjs_task(selected_examples,
                                          expected_labels,
                                          task_information)
        
        return 2, task, len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'], len(selected_examples) * app.config['CONTROLLER_LABELS_PER_QUESTION'] * next_category['price']

    elif best_task_category == 0:
        print "choosing the RECALL category"
        sys.stdout.flush()
        
        next_category = app.config['EXAMPLE_CATEGORIES'][0]
        
        task = make_recall_crowdjs_task(task_information)
        
        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE']
        return 0, task, num_hits, num_hits * next_category['price']
    
    elif best_task_category == 1:
        print "choosing the PRECISION category"
        sys.stdout.flush()

        next_category = app.config['EXAMPLE_CATEGORIES'][1]

        #positive_examples = []

        generate_task_ids = categories_to_examples[0]
        positive_examples, negative_examples = split_examples(
                generate_task_ids,
                [0 for i in generate_task_ids],
                ['all'])
        #for training_example_set, training_label_set in zip(
        #        training_examples, training_labels):
        #    for training_example, training_label in zip(
        #            training_example_set, training_label_set):
        #        if training_label == 1:
        #            positive_examples.append(training_example)

        num_hits = app.config['CONTROLLER_GENERATE_BATCH_SIZE'] * app.config[
            'CONTROLLER_NUM_MODIFY_TASKS_PER_SENTENCE']

        selected_positive_examples = sample(positive_examples, num_hits)
        
        
        task = make_precision_crowdjs_task(selected_positive_examples,
                                           task_information)
        
        return 1, task, num_hits, num_hits * next_category['price']
