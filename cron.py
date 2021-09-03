"""Cron jobs. Currently just minor cleanup tasks.
"""
from builtins import range
import datetime
import itertools
import logging

from flask import g
from flask.views import View
from google.cloud import ndb
import requests

from blogger import Blogger
from flask_background import app
from flickr import Flickr
from mastodon import Mastodon
import models
from models import Source
from twitter import Twitter
import util

CIRCLECI_TOKEN = util.read('circleci_token')
TWITTER_API_USER_LOOKUP = 'users/lookup.json?screen_name=%s'
TWITTER_USERS_PER_LOOKUP = 100  # max # of users per API call


@app.route('/cron/replace_poll_tasks')
def replace_poll_tasks():
  """Finds sources missing their poll tasks and adds new ones."""
  now = datetime.datetime.now()
  queries = [cls.query(Source.features == 'listen', Source.status == 'enabled')
             for cls in models.sources.values() if cls.AUTO_POLL]
  for source in itertools.chain(*queries):
    age = now - source.last_poll_attempt
    if age > max(source.poll_period() * 2, datetime.timedelta(hours=2)):
      logging.info('%s last polled %s ago. Adding new poll task.',
                   source.bridgy_url(), age)
      util.add_poll_task(source)

  return ''


@app.route('/cron/update_twitter_pictures')
def update_twitter_pictures():
  """Finds :class:`Twitter` sources with new profile pictures and updates them.

  https://github.com/snarfed/granary/commit/dfc3d406a20965a5ed14c9705e3d3c2223c8c3ff
  http://indiewebcamp.com/Twitter#Profile_Image_URLs
  """
  g.TRANSIENT_ERROR_HTTP_CODES = (Twitter.TRANSIENT_ERROR_HTTP_CODES +
                                  Twitter.RATE_LIMIT_HTTP_CODES)
  sources = {source.key_id(): source for source in Twitter.query()}
  if not sources:
    return

  # just auth as me or the first user. TODO: use app-only auth instead.
  auther = sources.get('schnarfed') or list(sources.values())[0]
  usernames = list(sources.keys())
  users = []
  for i in range(0, len(usernames), TWITTER_USERS_PER_LOOKUP):
    username_batch = usernames[i:i + TWITTER_USERS_PER_LOOKUP]
    url = TWITTER_API_USER_LOOKUP % ','.join(username_batch)
    try:
      users += auther.gr_source.urlopen(url)
    except Exception as e:
      code, body = util.interpret_http_exception(e)
      if not (code == '404' and len(username_batch) == 1):
        # 404 for a single user means they deleted their account. otherwise...
        raise

  for user in users:
    source = sources.get(user['screen_name'])
    if source:
      new_actor = auther.gr_source.user_to_actor(user)
      maybe_update_picture(source, new_actor)

  return 'OK'


class UpdatePictures(View):
  """Finds sources with new profile pictures and updates them."""
  SOURCE_CLS = None

  def source_query(self):
    return self.SOURCE_CLS.query()

  @classmethod
  def user_id(cls, source):
    return source.key_id()

  def dispatch_request(self):
    for source in self.source_query():
      if source.features and source.status != 'disabled':
        logging.debug('checking for updated profile pictures for: %s',
                      source.bridgy_url())
        try:
          actor = source.gr_source.get_actor(self.user_id(source))
        except BaseException as e:
          # Mastodon API returns HTTP 404 for deleted (etc) users, and
          # often one or more users' Mastodon instances are down.
          code, _ = util.interpret_http_exception(e)
          if code:
            continue
        maybe_update_picture(source, actor)

    return 'OK'


class UpdateFlickrPictures(UpdatePictures):
  """Finds :class:`Flickr` sources with new profile pictures and updates them.
  """
  SOURCE_CLS = Flickr

  def dispatch_request(self):
    g.TRANSIENT_ERROR_HTTP_CODES = (Flickr.TRANSIENT_ERROR_HTTP_CODES +
                                    Flickr.RATE_LIMIT_HTTP_CODES)
    return super().dispatch_request()


class UpdateMastodonPictures(UpdatePictures):
  """Finds :class:`Mastodon` sources with new profile pictures and updates them.
  """
  SOURCE_CLS = Mastodon

  def dispatch_request(self):
    g.TRANSIENT_ERROR_HTTP_CODES = (Mastodon.TRANSIENT_ERROR_HTTP_CODES +
                                    Mastodon.RATE_LIMIT_HTTP_CODES)
    return super().dispatch_request()

  @classmethod
  def user_id(cls, source):
    return source.auth_entity.get().user_id()


# class UpdateBloggerPictures(UpdatePictures):
#   """Finds :class:`Blogger` sources with new profile pictures and updates them.
#   """
#   SOURCE_CLS = Blogger

#   # TODO: no granary.Blogger!


def maybe_update_picture(source, new_actor):
  if not new_actor:
    return False
  new_pic = new_actor.get('image', {}).get('url')
  if not new_pic or source.picture == new_pic:
    logging.info(f'No new picture found for {source.bridgy_url()}')
    return

  @ndb.transactional()
  def update():
    src = source.key.get()
    src.picture = new_pic
    src.put()

  logging.info(f'Updating profile picture for {source.bridgy_url()} from {source.picture} to {new_pic}')
  update()


@app.route('/cron/build_circle')
def build_circle():
  """Trigger CircleCI to build and test the main branch.

  ...to run twitter_live_test.py, to check that scraping likes is still working.
  """
  resp = requests.post('https://circleci.com/api/v1.1/project/github/snarfed/bridgy/tree/main?circle-token=%s' % CIRCLECI_TOKEN)
  resp.raise_for_status()
  return 'OK'


app.add_url_rule('/cron/update_flickr_pictures',
                 view_func=UpdateFlickrPictures.as_view('update_flickr_pictures'))
app.add_url_rule('/cron/update_mastodon_pictures',
                 view_func=UpdateMastodonPictures.as_view('update_mastodon_pictures'))
