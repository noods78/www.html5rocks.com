# -*- coding: utf-8 -*-
# Copyright 2012 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License")
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
#

__author__ = 'ericbidelman@html5rocks.com (Eric Bidelman)'

# Standard Imports
import datetime
import glob
import logging
import os
import re
import urllib2
import webapp2
import yaml

# App libs.
import settings
import models

# Libraries
import html5lib
from html5lib import treebuilders, treewalkers

from django.template.loader import render_to_string
from django.utils import feedgenerator
from django.utils import simplejson
from django.utils import translation
from django.utils.translation import ugettext as _

# Google App Engine Imports
#from google.appengine.api import datastore_errors
from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.ext import db


class ContentHandler(webapp2.RequestHandler):

  BASEDIR = os.path.dirname(__file__)
  FEED_RESULTS_LIMIT = 20
  FEATURE_PAGE_WHATS_NEW_LIMIT = 10

  def get_language(self):
    lang_match = re.match("^/(\w{2,3})(?:/|$)", self.request.path)
    return lang_match.group(1) if lang_match else None
    
  def activate_language(self, language_code):
    self.locale = language_code or settings.LANGUAGE_CODE
    logging.info("Set Language as %s" % self.locale)
    translation.activate( self.locale )

  def browser(self):
    return str(self.request.headers['User-Agent'])

  def is_awesome_mobile_device(self):
    browser = self.browser()
    return browser.find('Android') != -1 or browser.find('iPhone') != -1

  def get_toc(self, path):
    # Only have TOC on tutorial pages. Don't do work for others.
    if not (re.search('/tutorials', path) or re.search('/mobile', path)):
      return ''

    toc = memcache.get('%s|toc|%s' % (settings.MEMCACHE_KEY_PREFIX, path))
    if toc is None or not self.request.cache:
      template_text = render_to_string(path, {})

      parser = html5lib.HTMLParser(tree=treebuilders.getTreeBuilder("dom"))
      dom_tree = parser.parse(template_text)
      walker = treewalkers.getTreeWalker("dom")
      stream = walker(dom_tree)
      toc = []
      current = None
      for element in stream:
        if element['type'] == 'StartTag':
          if element['name'] in ['h2', 'h3', 'h4']:
            for attr in element['data']:
              if attr[0] == 'id':
                current = {
                  'level' : int(element['name'][-1:]) - 1,
                  'id' : attr[1]
                }
        elif element['type'] == 'Characters' and current is not None:
          current['text'] = element['data']
        elif element['type'] == 'EndTag' and current is not None:
          toc.append(current)
          current = None
      memcache.set('%s|toc|%s' % (settings.MEMCACHE_KEY_PREFIX, path), toc, 3600)

    return toc

  def get_feed(self, path):
    articles = memcache.get('%s|feed|%s' % (settings.MEMCACHE_KEY_PREFIX, path))
    if articles is None or not self.request.cache:
      # DB query is memcached in get_all(). Limit to last several results
      tutorials = models.Resource.get_all(order='-publication_date',
                                          limit=self.FEED_RESULTS_LIMIT)

      articles = []
      for tut in tutorials:
        article = {}
        article['title'] = tut.title
        article['id'] = '-'.join(tut.title.lower().split())
        article['href'] = tut.url
        article['description'] = tut.description
        article['author_id'] = tut.author.key().name()
        if tut.second_author:
          article['second_author'] = tut.second_author.key().name()
        article['pubdate'] = datetime.datetime.strptime(
                                 str(tut.publication_date), '%Y-%m-%d')
        article['categories'] = []
        for tag in tut.tags:
          article['categories'].append(tag)

        articles.append(article)

      # Cache feed for 24hrs.
      memcache.set(
          '%s|feed|%s' % (settings.MEMCACHE_KEY_PREFIX, path), articles, 86400)

    return articles

  def render(self, data={}, template_path=None, status=None,
             message=None, relpath=None):
    if status is not None and status != 200:
      self.response.set_status(status, message)

      # Check if we have a customize error page (template) to display.
      if template_path is None:
        logging.error(message)
        self.response.set_status(status, message)
        self.response.out.write(message)
        return

    current = ''
    if relpath is not None:
      current = relpath.split('/')[0].split('.')[0]

    # Strip out language code from path. Urls changed for i18n work and correct
    # disqus comment thread won't load with the changed urls.
    path_no_lang = re.sub('^\/\w{2,3}(?:/|$)?', '', self.request.path, 1)
    logging.info("path after removing lang: %s" % path_no_lang)

    pagename = ''
    if path_no_lang == '':
      pagename = 'home'
    else:
      pagename = re.sub('\/', '-', path_no_lang)
      pagename = re.sub('/$|-$', '', pagename)
      pagename = re.sub('^-', '', pagename)

    # Add template data to every request.
    template_data = {
      'toc': self.get_toc(template_path),
      'self_url': self.request.url,
      'self_pagename': pagename,
      'host': '%s://%s' % (self.request.scheme, self.request.host),
      'is_mobile': self.is_awesome_mobile_device(),
      'current': current,
      'prod': settings.PROD
    }

    # If the tutorial contains a social URL override, use it.
    template_data['disqus_url'] = template_data['host'] + '/' + path_no_lang
    if data.get('tut') and data['tut'].social_url:
      template_data['disqus_url'] = (template_data['host'] +
                                     data['tut'].social_url)

    # Request was for an Atom feed. Render one!
    if self.request.path.endswith('.xml'):
      self.render_atom_feed(template_path, self.get_feed(template_path))
      return

    template_data.update(data)
    if not 'category' in template_data:
      template_data['category'] = _('this feature')

    # Add GDL url.
    # TODO: memcache this db query.
    template_data['gdl_page_url'] = ''
    live_data = models.LiveData.all().get() # Return first result.

    # Show banner if we have a URL and are under 60 minutes since it was saved.
    if (live_data and
        (datetime.datetime.now() - live_data.updated).seconds / 60 < 60):
      template_data['gdl_page_url'] = live_data.gdl_page_url

    # Add CORS support entire site.
    self.response.headers.add_header('Access-Control-Allow-Origin', '*')
    self.response.headers.add_header('X-UA-Compatible', 'IE=Edge,chrome=1')
    self.response.out.write(render_to_string(template_path, template_data))

  def render_atom_feed(self, template_path, data):
    prefix = '%s://%s' % (self.request.scheme, self.request.host)

    feed = feedgenerator.Atom1Feed(
        title=_(u'HTML5Rocks - Posts & Tutorials'),
        link=prefix,
        description=_(u'A resource for developers looking to put HTML5 to use '
                      'today, including information on specific features and '
                      'when to use them in your apps.'),
        language=u'en'
        )
    for tut in data:
      author_name = unicode(tut['author_id'])
      if 'second_author' in tut:
        author_name += ',' + tut['second_author']
      feed.add_item(
          title=unicode(tut['title']),
          link=prefix + tut['href'],
          description=unicode(tut['description']),
          pubdate=tut['pubdate'],
          author_name=author_name,
          categories=tut['categories']
          )
    self.response.headers.add_header('Content-Type', 'application/atom+xml')
    self.response.out.write(feed.writeString('utf-8'))

  def _set_cache_param(self):
    # Render uncached verion of page with ?cache=1
    if self.request.get('cache', default_value='1') == '1':
      self.request.cache = True
    else:
      self.request.cache = False

  def get(self, relpath):

    self._set_cache_param()

    # Handle bug redirects before anything else, as it's trivial.
    if relpath == 'new-bug':
      return self.redirect('https://github.com/html5rocks/www.html5rocks.com/issues/new')

    # Handle humans before locale, to prevent redirect to /en/
    # (but still ensure it's dynamic, ie we can't just redirect to a static url)
    if relpath == 'humans.txt':
      self.response.headers['Content-Type'] = 'text/plain'
      sorted_profiles = models.get_sorted_profiles()
      return self.render(data={'sorted_profiles': sorted_profiles,
                               'profile_amount': len(sorted_profiles)},
                         template_path='content/humans.txt',
                         relpath=relpath)

    # Get the locale: if it's "None", redirect to English
    locale = self.get_language()
    if not locale:
      return self.redirect("/en/%s" % relpath, permanent=True)

    # If there is a locale specified but it has no leading slash, redirect
    if not relpath.startswith("%s/" % locale):
      return self.redirect("/%s/" % locale, permanent=True)

    # If we get here, is because the language is specified correctly, 
    # so let's activate it
    self.activate_language(locale)

    
    # Strip off leading `/[en|de|fr|...]/`
    relpath = re.sub('^/?\w{2,3}(?:/)?', '', relpath)

    # Are we looking for a feed?
    is_feed = self.request.path.endswith('.xml')

    logging.info('relpath: ' + relpath)
    # Setup handling of redirected article URLs: If a user tries to access an
    # article from a non-supported language, we'll redirect them to the
    # English version (assuming it exists), with a `redirect_from_locale` GET
    # param.
    redirect_from_locale = self.request.get('redirect_from_locale', '')
    if not re.match('[a-zA-Z]{2,3}$', redirect_from_locale):
      redirect_from_locale = False
    else:
      translation.activate(redirect_from_locale)
      redirect_from_locale = {
        'lang': redirect_from_locale,
        'msg': _('Sorry, this article isn\'t available in your native '
                 'language; we\'ve redirected you to the English version.')
      }
      translation.activate(locale)

    # Landing page or /tutorials|features|mobile|gaming|business\/?
    if ((relpath == '' or relpath[-1] == '/') or  # Landing page.
        (relpath[-1] != '/' and relpath in ['mobile', 'tutorials', 'features',
                                            'gaming', 'business'])):
      path = os.path.join('content', relpath, 'index.html')
    else:
      path = os.path.join('content', relpath)

    # Render the .html page if it exists. Otherwise, check that the Atom feed
    # the user is requesting has a corresponding .html page that exists.

    if (relpath == 'profiles' or relpath == 'profiles/'):
      profiles = models.get_sorted_profiles()
      for p in profiles:
        p['tuts_by_author'] = models.Resource.get_tutorials_by_author(p['id'])
      self.render(data={'sorted_profiles': profiles},
                  template_path='content/profiles.html', relpath=relpath)
    elif ((re.search('tutorials/.+', relpath) or
           re.search('mobile/.+', relpath) or
           re.search('gaming/.+', relpath) or
           re.search('business/.+', relpath) or
           re.search('tutorials/casestudies/.+', relpath))
          and not is_feed):
      # If this is an old-style mobile article or case study, redirect to the
      # new style.
      match = re.search(('(?P<type>mobile|tutorials/casestudies)'
                         '/(?P<slug>[a-z-_0-9]+).html$'), relpath)
      if match:
        logging.info("Redirecting from old-style URL to the new hotness.")
        return self.redirect('/%s/%s/%s/' % (locale, match.group('type'),
                                             match.group('slug')))

      # If no trailing / (e.g. /tutorials/blah), redirect to /tutorials/blah/.
      if (relpath[-1] != '/' and not relpath.endswith('.html')):
        return self.redirect(self.request.url + '/')

      # Tutorials look like this on the filesystem:
      #
      #   .../tutorials +
      #                 |
      #                 +-- article-slug  +
      #                 |                 |
      #                 |                 +-- en  +
      #                 |                 |       |
      #                 |                 |       +-- index.html
      #                 ...
      #
      # So, to determine if an HTML page exists for the requested language
      # `split` the file's path, add in the locale, and check existence:
      logging.info('Building request for `%s` in locale `%s`', path, locale)
      (dir, filename) = os.path.split(path)
      if os.path.isfile(os.path.join(dir, locale, filename)):
        # Lookup tutorial by its url. Return the first one that matches.
        # get_all() not used because we don't care about caching on individual
        # tut page.
        tut = models.Resource.all().filter('url =', '/' + relpath).get()

        # If tutorial is marked as draft, redirect and don't show it.
        if tut and tut.draft:
          return self.redirect('/tutorials')

        # Localize title and description.
        if tut:
          if tut.title:
            tut.title = _(tut.title)
          if tut.description:
            tut.description = _(tut.description)

        # Gather list of localizations by globbing matching directories, then
        # stripping out the current locale and 'static'. Once we have a list,
        # convert it to a series of dictionaries containing the localization's
        # path and name:
        langs = {
          'de': 'Deutsch',
          'en': 'English',
          'fr': 'Français',
          'es': 'Español',
          'ja': '日本語',
          'pt': 'Português (Brasil)',
          'ru': 'Pусский',
          'zh': '中文 (简体)'
        }
        loc_list = []
        for d in glob.glob(os.path.join(dir, '*', 'index.html')):
          loc = os.path.basename(os.path.dirname(d))
          if loc not in [locale, 'static']:
            loc_list.append({'path': '/%s/%s' % (loc, relpath),
                             'lang': langs[loc]})

        data = {
          'tut': tut,
          'localizations': loc_list,
          'redirect_from_locale': redirect_from_locale
        }
        self.render(template_path=os.path.join(dir, locale, filename),
                    data=data, relpath=relpath)

      # If the localized file doesn't exist, and the locale isn't English, look
      # for an english version of the file, and redirect the user there if
      # it's found:
      elif os.path.isfile( os.path.join(dir, 'en', filename)):
        return self.redirect("/en/%s?redirect_from_locale=%s" % (relpath,
                                                                 locale))
    elif os.path.isfile(path):
      #TODO(ericbidelman): Don't need these tutorial/update results for query.
      if relpath in ['mobile', 'gaming', 'business']:
        results = TagsHandler().get_as_db(
            relpath, limit=self.FEATURE_PAGE_WHATS_NEW_LIMIT)
      else:
        results = models.Resource.get_all(order='-publication_date')

      tutorials = [] # List of final result set.
      authors = [] # List of authors related to the result set.
      for r in results:
        resource_type = [x for x in r.tags if x.startswith('type:')]
        if len(resource_type):
          resource_type = resource_type[0].replace('type:', '')

        if r.url.startswith('/'):
          # Localize title and description if article is localized.
          filepath = os.path.join(self.BASEDIR, 'content', r.url[1:], self.locale,
                                  'index.html')
          if os.path.isfile(filepath):
            if r.title:
              r.title = _(r.title)
            if r.description:
              r.description = _(r.description)
          # Point the article to the localized version, regardless.
          r.url = "/%s%s" % (self.locale, r.url)

        tutorials.append(r)
        tutorials[-1].classes = [x.replace('class:', '') for x in r.tags
                                 if x.startswith('class:')]
        tutorials[-1].tags = [x for x in r.tags
            if not (x.startswith('class:') or x.startswith('type:'))]
        tutorials[-1].type = resource_type

        #TODO(ericbidelman): Probably don't need author for every result query.
        authors.append(r.author)

      # Remove duplicate authors from the list.
      author_dict = {}
      for a in authors:
        author_dict[a.key().name()] = a
      authors = author_dict.values()

      self.render(
          data={'tutorials': tutorials, 'authors': authors}, template_path=path,
                relpath=relpath)

    elif os.path.isfile(path[:path.rfind('.')] + '.html'):
      self.render(data={}, template_path=path[:path.rfind('.')] + '.html',
                  relpath=relpath)

    elif os.path.isfile(path + '.html'):
      category = relpath.replace('features/', '')
      updates = TagsHandler().get_as_db(
          'class:' + category, limit=self.FEATURE_PAGE_WHATS_NEW_LIMIT)
      for r in updates:
        if r.url.startswith('/'):
          # Localize title if article is localized.
          filepath = os.path.join(self.BASEDIR, 'content', r.url[1:], self.locale,
                                  'index.html')
          if r.url.startswith('/') and os.path.isfile(filepath) and r.title:
            r.title = _(r.title)
          # Point the article to the localized version, regardless.
          r.url = "/%s%s" % (self.locale, r.url)

      data = {
        'category': category,
        'updates': updates
      }
      if relpath == 'why':
        if os.path.isfile(os.path.join(path, locale, 'index.html')):
          data['local_content_path'] = os.path.join(relpath, locale, 'index.html')
        else:
          data['local_content_path'] = os.path.join(relpath, 'en', 'index.html')
        
      self.render(data=data, template_path=path + '.html', relpath=relpath)

    else:
      self.render(status=404, message='Page Not Found', template_path='404.html')

  def handle_exception(self, exception, debug_mode):
    if debug_mode:
      super(ContentHandler, self).handle_exception(exception, debug_mode)
    else:
      # Display a generic 500 error page.
      self.render(status=500, message='Server Error', template_path='500.html')


class DBHandler(ContentHandler):

  def _ImportBackupResources(self, file_name):
    f = file(os.path.dirname(__file__) + file_name, 'r')
    for res in yaml.load_all(f):
      try:
        author_key = models.Author.get_by_key_name(res['author_id'])
        author_key2 = None
        if 'author_id2' in res:
          author_key2 = models.Author.get_by_key_name(res['author_id2'])

        resource = models.Resource(
          title=res['title'],
          description=res.get('description') or None,
          author=author_key,
          second_author=author_key2,
          url=unicode(res['url']),
          social_url=unicode(res.get('social_url') or ''),
          browser_support=res.get('browser_support') or [],
          update_date=res.get('update_date'),
          publication_date=res['publication_date'],
          tags=res['tags'],
          draft=False # These are previous resources. Mark them as published.
          )
        resource.put()

      except TypeError:
        pass # TODO(ericbidelman): Not sure why this is throwing an error, but
             # ignore it, whatever it is.
    f.close()

  def _AddTestResources(self):
    #memcache.delete('tutorials')
    memcache.flush_all()
    self._ImportBackupResources('/database/tutorials.yaml')

  def _AddTestPlaygroundSamples(self):
    memcache.flush_all()
    self._ImportBackupResources('/database/playground.yaml')

  def _AddTestStudioSamples(self):
    memcache.flush_all()
    self._ImportBackupResources('/database/studio.yaml')

  def _AddTestAuthors(self):
    memcache.flush_all()
    f = file(os.path.dirname(__file__) + '/database/profiles.yaml', 'r')
    for profile in yaml.load_all(f):
      author = models.Author(
          key_name=unicode(profile['id']),
          given_name=unicode(profile['name']['given']),
          family_name=unicode(profile['name']['family']),
          org=unicode(profile['org']['name']),
          unit=unicode(profile['org']['unit']),
          city=profile['address']['locality'],
          state=profile['address']['region'],
          country=profile['address']['country'],
          google_account=str(profile.get('google')),
          twitter_account=profile.get('twitter'),
          email=profile['email'],
          lanyrd=profile.get('lanyrd', False),
          homepage=profile['homepage'],
          geo_location=db.GeoPt(profile['address']['lat'],
                                profile['address']['lon'])
          )
      author.put()
    f.close()

  def _NukeDB(self):
    authors = models.Author.all()
    for author in authors:
      author.delete()

    resources = models.Resource.all()
    for resource in resources:
      resource.delete()

    memcache.flush_all()

  # /database/resource
  # /database/resource/1234
  # /database/load_all
  # /database/drop_all
  # /database/author
  # /database/live
  def get(self, relpath, post_id=None):
    self._set_cache_param()

    if (relpath == 'live'):
      user = users.get_current_user()

      # Restrict access to this page to admins and whitelisted users. 
      if (not users.is_current_user_admin() and
          user.email() not in settings.WHITELISTED_USERS):
        return self.redirect('/')

      entity = models.LiveData.all().get()
      if entity:
        live_form = models.LiveForm(entity.to_dict(), initial={
            'gdl_page_url': entity.gdl_page_url
            })
      else:
        live_form = models.LiveForm()

      template_data = {
        'live_form': live_form
      }
      return self.render(data=template_data,
                         template_path='database/live.html',
                         relpath=relpath)

    elif (relpath == 'author'):
      # adds a new author information into DataStore.
      sorted_profiles = models.get_sorted_profiles(update_cache=True)
      template_data = {
        'sorted_profiles': sorted_profiles,
        'profile_amount': len(sorted_profiles),
        'author_form': models.AuthorForm()
      }
      return self.render(data=template_data,
                         template_path='database/author_new.html',
                         relpath=relpath)

    elif (relpath == 'drop_all'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')  
      self._NukeDB()

    elif (relpath == 'load_tutorials'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')  
      self._AddTestResources()

    elif (relpath == 'load_authors'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')  
      self._AddTestAuthors()

    elif (relpath == 'load_playground_samples'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')  
      self._AddTestPlaygroundSamples()

    elif (relpath == 'load_studio_samples'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')  
      self._AddTestStudioSamples()

    elif (relpath == 'load_all'):
      if settings.PROD:
        return self.response.out.write('Handler not allowed in production.')

      # TODO(ericbidelman): Make this async.
      self._AddTestAuthors()
      self._AddTestResources()
      self._AddTestPlaygroundSamples()
      self._AddTestStudioSamples()

    elif relpath == 'resource':
      tutorial_form = models.TutorialForm()

      if post_id: # /database/resource/1234
        post = models.Resource.get_by_id(int(post_id))
        if post:
          author_id = post.author.key().name()
          second_author_id = (post.second_author and
                              post.second_author.key().name())

          # Adjust browser support so it renders to the checkboxes correctly.
          browser_support = [b.capitalize() for b in post.browser_support]
          for b in browser_support:
            if len(b) == 2:
              browser_support[browser_support.index(b)] = b.upper()

          form_data = post.to_dict()
          form_data['tags'] = ', '.join(post.tags)
          form_data['author'] = author_id
          form_data['second_author'] = second_author_id or author_id
          form_data['browser_support'] = browser_support

          tutorial_form = models.TutorialForm(form_data)

      template_data = {
        'tutorial_form': tutorial_form,
        # get_all() not used b/c we don't care about caching on this page.
        'resources': (models.Resource.all().order('-publication_date')
                                     .fetch(limit=settings.MAX_FETCH_LIMIT)),
        'post_id': post_id and int(post_id) or ''
      }
      return self.render(data=template_data,
                         template_path='database/resource_new.html',
                         relpath=relpath)

    # Catch all to redirect to proper page.
    return self.redirect('/database/resource')

  def post(self, relpath):

    if relpath == 'live':
      # Get first (and only) result.
      live_data = models.LiveData.all().get()
      if live_data is None:
        live_data = models.LiveData()
      
      live_data.gdl_page_url = self.request.get('gdl_page_url') or None

      #if live_data.gdl_page_url is not None:
      live_data.put()

      return self.redirect('/database/live')

    elif relpath == 'author':
      try:
        given_name = self.request.get('given_name')
        family_name = self.request.get('family_name')
        author = models.Author(
            key_name=''.join([given_name, family_name]).lower(),
            given_name=given_name,
            family_name=family_name,
            org=self.request.get('org'),
            unit=self.request.get('unit'),
            city=self.request.get('city'),
            state=self.request.get('state'),
            country=self.request.get('country'),
            homepage=self.request.get('homepage') or None,
            google_account=self.request.get('google_account') or None,
            twitter_account=self.request.get('twitter_account') or None,
            email=self.request.get('email') or None,
            lanyrd=self.request.get('lanyrd') == 'on')
        lat = self.request.get('lat')
        lon = self.request.get('lon')
        if lat and lon:
          author.geo_location = db.GeoPt(float(lat), float(lon))

        author.put()

      except db.Error, e:
        # TODO: Doesn't repopulate lat/lng or return errors for it.
        form = models.AuthorForm(self.request.POST)
        if not form.is_valid():
          sorted_profiles = models.get_sorted_profiles(update_cache=True)
          template_data = {
            'sorted_profiles': sorted_profiles,
            'profile_amount': len(sorted_profiles),
            'author_form': form
          }
          return self.render(data=template_data,
                             template_path='database/author_new.html',
                             relpath=relpath)
      else:
        self.redirect('/database/author')

    elif relpath == 'resource':
      author_key = models.Author.get_by_key_name(self.request.get('author'))
      author_key2 = models.Author.get_by_key_name(
          self.request.get('second_author'))

      if author_key.key() == author_key2.key():
        author_key2 = None

      tags = (self.request.get('tags') or '').split(',')
      tags = [x.strip() for x in tags if x.strip()]

      browser_support = [x.lower() for x in
                         (self.request.get_all('browser_support') or [])]

      pub = datetime.datetime.strptime(
          self.request.get('publication_date'), '%Y-%m-%d')

      update_date = self.request.get('update_date') or None

      tutorial = None
      if self.request.get('post_id'):
        tutorial = models.Resource.get_by_id(int(self.request.get('post_id')))

      # Updating existing resource.
      if tutorial:
        try:
          #TODO: This is also hacky.
          tutorial.title = self.request.get('title')
          tutorial.description = self.request.get('description')
          tutorial.author = author_key
          tutorial.second_author = author_key2
          tutorial.url = self.request.get('url') or None
          tutorial.browser_support = browser_support
          tutorial.update_date = datetime.date.today()
          tutorial.publication_date = datetime.date(pub.year, pub.month,
                                                    pub.day)
          tutorial.tags = tags
          tutorial.draft = self.request.get('draft') == 'on'
          tutorial.social_url = unicode(self.request.get('social_url') or '')
        except TypeError:
          pass
      else:
        # Create new resource.
        try:
          tutorial = models.Resource(
              title=self.request.get('title'),
              description=self.request.get('description'),
              author=author_key,
              second_author=author_key2,
              url=self.request.get('url') or None,
              browser_support=browser_support,
              update_date=datetime.date.today(),
              publication_date=datetime.date(pub.year, pub.month, pub.day),
              tags=tags,
              draft=self.request.get('draft') == 'on',
              social_url=self.request.get('social_url') or None
              )
        except TypeError:
          pass

      tutorial.put()

    # TODO: Don't use flush_all. Use flush_all_async() or only purge tutorials.
    # Once new entry is saved, flush memcache.
    memcache.flush_all()

    return self.redirect('/database/')


class APIHandler(ContentHandler):

  def get(self, relpath):
    output = []

    if relpath == 'authors':
      profiles = {}
      for p in models.get_sorted_profiles(): # This query is memcached.
        profile_id = p['id']
        profiles[profile_id] = p
        geo_location = profiles[profile_id]['geo_location']
        profiles[profile_id]['geo_location'] = str(geo_location)
      output = profiles
    elif relpath == 'tutorials':
      output = TagsHandler()._query_to_serializable_list(
         TagsHandler().get_as_db('type:tutorial'))
    elif relpath == 'articles':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:article'))
    elif relpath == 'casestudies':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:casestudy'))
    elif relpath == 'demos':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:demo'))
    elif relpath == 'samples':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:sample'))
    elif relpath == 'presentations':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:presentation'))
    elif relpath == 'announcements':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:announcement'))
    elif relpath == 'videos':
      output = TagsHandler()._query_to_serializable_list(
          TagsHandler().get_as_db('type:video'))

    self.response.headers.add_header('Access-Control-Allow-Origin', '*')
    self.response.headers['Content-Type'] = 'application/json'
    self.response.out.write(simplejson.dumps(output))


class TagsHandler(ContentHandler):

  def _query_to_serializable_list(self, results):
    resources = []
    for r in results:
      second_author = None
      if r.second_author:
        second_author = r.second_author.key().name()

      update_date = r.update_date
      if update_date:
        update_date = r.update_date.isoformat()

      resources.append(r.to_dict())
      resources[-1]['publication_date'] = r.publication_date.isoformat()
      resources[-1]['update_date'] = update_date
      resources[-1]['author'] = r.author.key().name()
      resources[-1]['second_author'] = second_author

    return resources

  def _get(self, tag, order=None, limit=None):
    tag = urllib2.unquote(tag)

    order = order or '-publication_date'

    # DB query is memcached in get_all().
    return models.Resource.get_all(order=order, qfilter=('tags =', tag),
                                   limit=limit)

  # /tags/json/type:demo
  # /tags/json/class:file_access
  # /tags/json/dnd
  # /tags/db/dnd
  def get(self, format, tag):
    if format == 'json':
      return self.get_as_json(tag)
    elif format == 'db':
      return self.get_as_db(tag)

  def get_as_json(self, tag):
    results = self._get(tag)

    resources = self._query_to_serializable_list(results)

    self.response.headers.add_header('Access-Control-Allow-Origin', '*')
    self.response.headers['Content-Type'] = 'application/json'
    self.response.out.write(simplejson.dumps(resources))
    return

  def get_as_db(self, tag, order=None, limit=None):
    return self._get(tag, order=order, limit=limit)


def handle_404(request, response, exception):
  response.write('Oops! Not Found.')
  response.set_status(404)

def handle_500(request, response, exception):
  logging.exception(exception)
  response.write('Oops! Internal Server Error.')
  response.set_status(500)


# App URL routes.
routes = [
  ('/api/(.*)', APIHandler),
  ('/database/(.*)/(.*)', DBHandler),
  ('/database/(.*)', DBHandler),
  ('/tags/(.*)/(.*)', TagsHandler),
  ('/(.*)', ContentHandler)
]

app = webapp2.WSGIApplication(routes, debug=settings.DEBUG)
app.error_handlers[404] = handle_404
if settings.PROD and not settings.DEBUG:
  app.error_handlers[500] = handle_500
