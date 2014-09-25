#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright 2014 Zuza Software Foundation
# Copyright 2013-2014 Evernote Corporation
#
# This file is part of Pootle.
#
# Pootle is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# translate is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with translate; if not, write to the Free Software
# Foundation, Inc., 59

__all__ = ('TreeItem', 'CachedMethods',)

from datetime import datetime
from functools import wraps

from translate.filters.decorators import Category

from django.conf import settings
from django.core.cache import cache
from django.utils.encoding import iri_to_uri

from django_rq import job

from pootle.core.log import log
from pootle.core.url_helpers import get_all_pootle_paths, split_pootle_path
from pootle_misc.checks import get_qualitychecks_by_category
from pootle_misc.util import datetime_min, dictsum


def statslog(function):
    @wraps(function)
    def _statslog(instance, *args, **kwargs):
        start = datetime.now()
        result = function(instance, *args, **kwargs)
        end = datetime.now()
        log("%s(%s)\t%s\t%s" % (function.__name__, ', '.join(args), end - start,
                                instance.get_cachekey()))
        return result
    return _statslog


class CachedMethods(object):
    """Cached method names."""
    CHECKS = 'get_checks'
    TOTAL = 'get_total_wordcount'
    TRANSLATED = 'get_translated_wordcount'
    FUZZY = 'get_fuzzy_wordcount'
    LAST_ACTION = 'get_last_action'
    SUGGESTIONS = 'get_suggestion_count'
    MTIME = 'get_mtime'
    LAST_UPDATED = 'get_last_updated'

    # Check refresh_stats command when add a new CachedMethod

    @classmethod
    def get_all(self):
        return [getattr(self, x) for x in
                filter(lambda x: x[:2] != '__' and x != 'get_all', dir(self))]


class TreeItem(object):
    def __init__(self, *args, **kwargs):
        self.children = None
        self.initialized = False
        self._dirty_cache = set()
        super(TreeItem, self).__init__()

    def get_children(self):
        """This method will be overridden in descendants"""
        return []

    def get_parents(self):
        """This method will be overridden in descendants"""
        return []

    def get_cachekey(self):
        """This method will be overridden in descendants"""
        raise NotImplementedError('`get_cachekey()` not implemented')

    def _get_total_wordcount(self):
        """This method will be overridden in descendants"""
        return 0

    def _get_translated_wordcount(self):
        """This method will be overridden in descendants"""
        return 0

    def _get_fuzzy_wordcount(self):
        """This method will be overridden in descendants"""
        return 0

    def _get_suggestion_count(self):
        """This method will be overridden in descendants"""
        return 0

    def _get_checks(self):
        """This method will be overridden in descendants"""
        return {'unit_count': 0, 'checks': {}}

    def _get_last_action(self):
        """This method will be overridden in descendants"""
        return {'id': 0, 'mtime': 0, 'snippet': ''}

    def _get_mtime(self):
        """This method will be overridden in descendants"""
        return datetime_min

    def _get_last_updated(self):
        """This method will be overridden in descendants"""
        return {'id': 0, 'creation_time': 0, 'snippet': ''}

    def initialize_children(self):
        if not self.initialized:
            self.children = self.get_children()
            self.initialized = True

    def set_cached_value(self, name, value,
                         timeout=settings.OBJECT_CACHE_TIMEOUT):
        key = iri_to_uri(self.get_cachekey() + ":" + name)
        return cache.set(key, value, timeout)

    def get_cached_value(self, name):
        key = iri_to_uri(self.get_cachekey() + ":" + name)
        return cache.get(key)

    def _calc_sum(self, name):
        self.initialize_children()
        method = getattr(self, '_%s' % name)
        return (method() +
                sum([item.get_cached(name) for item in self.children]))

    def _calc_last_action(self):
        self.initialize_children()

        return max(
            [self._get_last_action()] +
            [item.get_cached(CachedMethods.LAST_ACTION)
             for item in self.children],
            key=lambda x: x['mtime'] if 'mtime' in x else 0
        )

    def _calc_mtime(self):
        """get latest modification time"""
        self.initialize_children()
        return max(
            [self._get_mtime()] +
            [item.get_cached(CachedMethods.MTIME) for item in self.children]
        )

    def _calc_last_updated(self):
        """get last updated"""
        self.initialize_children()
        return max(
            [self._get_last_updated()] +
            [item.get_cached(CachedMethods.LAST_UPDATED)
             for item in self.children],
            key=lambda x: x['creation_time'] if 'creation_time' in x else 0
        )

    def _calc_checks(self):
        result = self._get_checks()
        self.initialize_children()
        for item in self.children:
            item_res = item.get_cached(CachedMethods.CHECKS)
            result['checks'] = dictsum(result['checks'], item_res['checks'])
            result['unit_count'] += item_res['unit_count']

        return result

    def _calc(self, name):
        return {
            CachedMethods.TOTAL: self._calc_sum(CachedMethods.TOTAL),
            CachedMethods.TRANSLATED: self._calc_sum(CachedMethods.TRANSLATED),
            CachedMethods.FUZZY: self._calc_sum(CachedMethods.FUZZY),
            CachedMethods.SUGGESTIONS: self._calc_sum(CachedMethods.SUGGESTIONS),
            CachedMethods.LAST_ACTION: self._calc_last_action(),
            CachedMethods.LAST_UPDATED: self._calc_last_updated(),
            CachedMethods.CHECKS: self._calc_checks(),
            CachedMethods.MTIME: self._calc_mtime(),
        }.get(name, None)

    @statslog
    def update_cached(self, name):
        """calculate total wordcount statistics and update cached value"""
        self.set_cached_value(name, self._calc(name))

    def get_cached(self, name):
        """get total wordcount statistics from cache or calculate for
        virtual resources
        """
        if getattr(self, 'no_cache', False):
            return self._calc(name)

        result = self.get_cached_value(name)
        if result is None:
            log("cache miss %s for %s(%s)" %
                (name, self.get_cachekey(), self.__class__))

        return result

    def get_stats(self, include_children=True):
        """get stats for self and - optionally - for children"""
        self.initialize_children()

        result = {
            'total': self.get_cached(CachedMethods.TOTAL),
            'translated': self.get_cached(CachedMethods.TRANSLATED),
            'fuzzy': self.get_cached(CachedMethods.FUZZY),
            'suggestions': self.get_cached(CachedMethods.SUGGESTIONS),
            'lastaction': self.get_cached(CachedMethods.LAST_ACTION),
            'critical': self.get_error_unit_count(),
            'lastupdated': self.get_cached(CachedMethods.LAST_UPDATED)
        }

        if include_children:
            result['children'] = {}
            for item in self.children:
                code = (self._get_code(item) if hasattr(self, '_get_code')
                                             else item.code)
                result['children'][code] = item.get_stats(False)

        return result

    def refresh_stats(self, include_children=True):
        """refresh cached stats for self and for children"""
        self.initialize_children()

        if include_children:
            for item in self.children:
                # note that refresh_stats for a Store object does nothing
                item.refresh_stats()

        for name in CachedMethods.get_all():
            self.update_cached(name)

    def get_error_unit_count(self):
        check_stats = self.get_cached(CachedMethods.CHECKS)

        return getattr(check_stats, 'unit_count', 0)

    def get_critical_url(self):
        critical = ','.join(get_qualitychecks_by_category(Category.CRITICAL))
        return self.get_translate_url(check=critical)

    def mark_dirty(self, *args):
        """Mark cached method names for this TreeItem as dirty"""
        for key in args:
            self._dirty_cache.add(key)

    def mark_all_dirty(self):
        """Mark all cached method names for this TreeItem as dirty"""
        all_cache_methods = CachedMethods.get_all()
        self._dirty_cache = set(all_cache_methods)

    def _clear_cache(self, keys, parents=True, children=False):
        itemkey = self.get_cachekey()
        for key in keys:
            cachekey = iri_to_uri(itemkey + ":" + key)
            cache.delete(cachekey)
        if keys:
            log("%s deleted from %s cache" % (keys, itemkey))

        if parents:
            parents = self.get_parents()
            for p in parents:
                p._clear_cache(keys, parents=parents, children=False)

        if children:
            self.initialize_children()
            for item in self.children:
                item._clear_cache(keys, parents=False, children=True)

    def clear_dirty_cache(self, parents=True, children=False):
        self._clear_cache(self._dirty_cache,
                          parents=parents, children=children)
        self._dirty_cache = set()

    def clear_all_cache(self, children=True, parents=True):
        all_cache_methods = CachedMethods.get_all()
        self.mark_dirty(*all_cache_methods)
        self.clear_dirty_cache(children=children, parents=parents)

    ################ Update stats in Redis Queue Worker process ###############

    def update_dirty_cache(self):
        """Add a RQ job which updates dirty cached stats of current TreeItem
        to the default queue
        """
        _dirty = self._dirty_cache.copy()
        if _dirty:
            self._dirty_cache = set()
            update_cache.delay(self, _dirty)

    def update_all_cache(self):
        """Add a RQ job which updates all cached stats of current TreeItem
        to the default queue
        """
        self.mark_all_dirty()
        self.update_dirty_cache()

    def _update_cache(self, keys):
        """Update dirty cached stats of current TreeItem"""
        for key in keys:
            self.update_cached(key)

        parents = self.get_parents()
        for p in parents:
            p._update_cache(keys)

    def update_parent_cache(self, exclude_self=False):
        """Update dirty cached stats for a all parents of the current TreeItem"""
        all_cache_methods = CachedMethods.get_all()
        parents = self.get_parents()
        for p in parents:
            p.initialize_children()
            if exclude_self:
                p.children = filter(
                    lambda x: (x.id != self.id and
                               x.__class__ == self.__class__),
                    p.children
                )
            update_cache.delay(p, all_cache_methods)


@job
def update_cache(instance, keys):
    """RQ job"""
    instance._update_cache(keys)
