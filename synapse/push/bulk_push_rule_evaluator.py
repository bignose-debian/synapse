# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import ujson as json

from twisted.internet import defer

import baserules
from push_rule_evaluator import PushRuleEvaluatorForEvent

from synapse.api.constants import EventTypes


logger = logging.getLogger(__name__)


def decode_rule_json(rule):
    rule['conditions'] = json.loads(rule['conditions'])
    rule['actions'] = json.loads(rule['actions'])
    return rule


@defer.inlineCallbacks
def _get_rules(room_id, user_ids, store):
    rules_by_user = yield store.bulk_get_push_rules(user_ids)
    rules_by_user = {
        uid: baserules.list_with_base_rules([
            decode_rule_json(rule_list)
            for rule_list in rules_by_user.get(uid, [])
        ])
        for uid in user_ids
    }
    defer.returnValue(rules_by_user)


@defer.inlineCallbacks
def evaluator_for_room_id(room_id, store):
    users = yield store.get_users_in_room(room_id)
    rules_by_user = yield _get_rules(room_id, users, store)

    defer.returnValue(BulkPushRuleEvaluator(
        room_id, rules_by_user, users, store
    ))


class BulkPushRuleEvaluator:
    """
    Runs push rules for all users in a room.
    This is faster than running PushRuleEvaluator for each user because it
    fetches all the rules for all the users in one (batched) db query
    rather than doing multiple queries per-user. It currently uses
    the same logic to run the actual rules, but could be optimised further
    (see https://matrix.org/jira/browse/SYN-562)
    """
    def __init__(self, room_id, rules_by_user, users_in_room, store):
        self.room_id = room_id
        self.rules_by_user = rules_by_user
        self.users_in_room = users_in_room
        self.store = store

    @defer.inlineCallbacks
    def action_for_event_by_user(self, event, handler):
        actions_by_user = {}

        users_dict = yield self.store.are_guests(self.rules_by_user.keys())

        filtered_by_user = yield handler._filter_events_for_clients(
            users_dict.items(), [event]
        )

        evaluator = PushRuleEvaluatorForEvent(event, len(self.users_in_room))

        condition_cache = {}

        member_state = yield self.store.get_state_for_event(
            event.event_id,
        )

        display_names = {}
        for ev in member_state.values():
            nm = ev.content.get("displayname", None)
            if nm and ev.type == EventTypes.Member:
                display_names[ev.state_key] = nm

        for uid, rules in self.rules_by_user.items():
            display_name = display_names.get(uid, None)

            filtered = filtered_by_user[uid]
            if len(filtered) == 0:
                continue

            for rule in rules:
                if 'enabled' in rule and not rule['enabled']:
                    continue

                matches = _condition_checker(
                    evaluator, rule['conditions'], uid, display_name, condition_cache
                )
                if matches:
                    actions = [x for x in rule['actions'] if x != 'dont_notify']
                    if actions:
                        actions_by_user[uid] = actions
                    break
        defer.returnValue(actions_by_user)


def _condition_checker(evaluator, conditions, uid, display_name, cache):
    for cond in conditions:
        _id = cond.get("_id", None)
        if _id:
            res = cache.get(_id, None)
            if res is False:
                break
            elif res is True:
                continue

        res = evaluator.matches(cond, uid, display_name, None)
        if _id:
            cache[_id] = res

        if res is False:
            return False

    return True