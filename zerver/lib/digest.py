from __future__ import absolute_import

from collections import defaultdict
import datetime

from django.db.models import Q
from django.template import loader

from zerver.lib.actions import build_message_list, hashchange_encode, \
    send_future_email
from zerver.models import UserProfile, UserMessage, Recipient, Stream, \
    Subscription

# Digests accumulate 4 types of interesting traffic for a user:
# 1. Missed PMs
# 2. New streams
# 3. New users
# 4. Interesting stream traffic, as determined by the longest and most
#    diversely comment upon topics.

def gather_hot_conversations(user_profile, stream_messages):
    # Gather stream conversations of 2 types:
    # 1. long conversations
    # 2. conversations where many different people participated
    #
    # Returns a list of dictionaries containing the templating
    # information for each hot conversation.

    conversation_length = defaultdict(int)
    conversation_diversity = defaultdict(set)
    for user_message in stream_messages:
        key = (user_message.message.recipient.type_id,
               user_message.message.subject)
        conversation_diversity[key].add(
            user_message.message.sender.full_name)
        conversation_length[key] += 1

    diversity_list = conversation_diversity.items()
    diversity_list.sort(key=lambda entry: len(entry[1]), reverse=True)

    length_list = conversation_length.items()
    length_list.sort(key=lambda entry: entry[1], reverse=True)

    # Get up to the 4 best conversations from the diversity list
    # and length list, filtering out overlapping conversations.
    hot_conversations = [elt[0] for elt in diversity_list[:2]]
    for candidate, _ in length_list:
        if candidate not in hot_conversations:
            hot_conversations.append(candidate)
        if len(hot_conversations) >= 4:
            break

    hot_conversation_render_payloads = []
    for h in hot_conversations:
        stream_id, subject = h
        users = list(conversation_diversity[h])
        count = conversation_length[h]

        # We'll display up to 2 messages from the conversation.
        first_few_messages = [user_message.message for user_message in \
                                  stream_messages.filter(
                message__recipient__type_id=stream_id,
                message__subject=subject)[:2]]

        teaser_data = {"participants": users,
                       "count": count - len(first_few_messages),
                       "first_few_messages": build_message_list(
                user_profile, first_few_messages)}

        hot_conversation_render_payloads.append(teaser_data)
        return hot_conversation_render_payloads

def gather_new_users(user_profile, threshold):
    # Gather information on users in the realm who have recently
    # joined.
    new_users = list(UserProfile.objects.filter(
            realm=user_profile.realm, date_joined__gt=threshold,
            is_bot=False))
    user_names = [user.full_name for user in new_users]

    return len(user_names), user_names

def gather_new_streams(user_profile, threshold):
    new_streams = list(Stream.objects.filter(
            realm=user_profile.realm, date_created__gt=threshold))

    base_url = "https://zulip.com/#narrow/stream/"

    streams_html = []
    streams_plain = []

    for stream in new_streams:
        narrow_url = base_url + hashchange_encode(stream.name)
        stream_link = "<a href='%s'>%s</a>" % (narrow_url, stream.name)
        streams_html.append(stream_link)
        streams_plain.append(stream.name)

    return len(new_streams), {"html": streams_html, "plain": streams_plain}

def enough_traffic(unread_pms, hot_conversations, new_streams, new_users):
    if unread_pms or hot_conversations:
        # If you have any unread traffic, good enough.
        return True
    if new_streams and new_users:
        # If you somehow don't have any traffic but your realm did get
        # new streams and users, good enough.
        return True
    return False

def handle_digest_email(user_profile_id, cutoff):
    user_profile=UserProfile.objects.get(id=user_profile_id)
    # Convert from epoch seconds to a datetime object.
    cutoff = datetime.datetime.utcfromtimestamp(int(cutoff))

    all_messages = UserMessage.objects.filter(
        user_profile=user_profile,
        message__pub_date__gt=cutoff).order_by("message__pub_date")

    # Start building email template data.
    template_payload = {'name': user_profile.full_name}

    # Gather recent missed PMs, re-using the missed PM email logic.
    pms = all_messages.filter(~Q(message__recipient__type=Recipient.STREAM))

    # Show up to 4 missed PMs.
    pms_limit = 4

    template_payload['unread_pms'] = build_message_list(
        user_profile, [pm.message for pm in pms[:pms_limit]])
    template_payload['remaining_unread_pms_count'] = min(0, len(pms) - pms_limit)

    home_view_recipients = [sub.recipient for sub in \
                                Subscription.objects.filter(
            user_profile=user_profile, active=True, in_home_view=True)]

    stream_messages = all_messages.filter(
        message__recipient__type=Recipient.STREAM,
        message__recipient__in=home_view_recipients)

    # Gather hot conversations.
    template_payload["hot_conversations"] = gather_hot_conversations(
        user_profile, stream_messages)

    # Gather new streams.
    new_streams_count, new_streams = gather_new_streams(
        user_profile, cutoff)
    template_payload["new_streams"] = new_streams
    template_payload["new_streams_count"] = new_streams_count

    # Gather users who signed up recently.
    new_users_count, new_users = gather_new_users(
        user_profile, cutoff)
    template_payload["new_users"] = new_users

    text_content = loader.render_to_string(
        'zerver/emails/digest/digest_email.txt', template_payload)
    html_content = loader.render_to_string(
        'zerver/emails/digest/digest_email_html.txt', template_payload)

    recipients = [{'email': user_profile.email, 'name': user_profile.full_name}]
    subject = "While you've been gone: the Zulip Digest"
    sender = {'email': 'support@zulip.com', 'name': 'Zulip Support'}

    # We don't want to send emails containing almost no information.
    if enough_traffic(template_payload["unread_pms"],
                      template_payload["hot_conversations"],
                      new_streams_count, new_users_count):

        # Send now, through Mandrill.
        send_future_email(recipients, html_content, text_content, subject,
                          delay=datetime.timedelta(0), sender=sender,
                          tags=["digest-emails"])
