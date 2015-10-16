from kn.leden.mongo import db, SONWrapper, _id, son_property
import kn.leden.entities as Es
from kn.fotos.utils import resize_proportional, split_words
from django.conf import settings

from django.db.models import permalink

import os
import re
import Image
from PIL.ExifTags import TAGS
import random
import datetime
import os.path
import mimetypes
import subprocess
from collections import namedtuple


cache_tuple = namedtuple('cache_tuple', ('ext', 'mimetype', 'maxwidth', 'maxheight', 'quality'))

fcol = db['fotos']
lcol = db['fotoLocks']


def ensure_indices():
    fcol.ensure_index([('type', 1), ('oldId', 1)], sparse=True)
    fcol.ensure_index([('path', 1), ('name', 1)])
    fcol.ensure_index([('type', 1), ('path', 1),
                       ('random', 1), ('effectiveVisibility', 1)])
    fcol.ensure_index('tags', sparse=True)
    fcol.ensure_index([('caches', 1), ('type', 1)], sparse=True)
    fcol.ensure_index([('path', 1), ('effectiveVisibility', 1), ('name', 1)])
    fcol.ensure_index([('name', 'text'),
                       ('title', 'text'),
                       ('description', 'text'),
                       ('path', 1),
                       ('effectiveVisibility', 1)],
                      weights={'name': 100,
                               'comment': 50},
                      default_language="dutch",
                      sparse=True)

def entity(d):
    if d is None:
        return None
    return TYPE_MAP[d['type']](d)

def by_id(the_id):
    return entity(fcol.find_one({'_id': _id(the_id)}))

def by_oldId(_type, oldId):
    return entity(fcol.find_one({'type': _type, 'oldId': oldId}))

def by_path_and_name(p, n):
    return entity(fcol.find_one({'path': p, 'name': n}))

def by_path(p):
    bits = p.rsplit('/', 1)
    if len(bits) == 1:
        name = bits[0]
        pp = '' if name else None
    else:
        pp, name = bits
    return by_path_and_name(pp, name)

def is_admin(user):
    if user is None:
        return False
    return bool(user.cached_groups_names & frozenset(('fotocie', 'webcie')))

def actual_visibility(visibility):
    actual = frozenset(visibility)

    implies = {
        'world': frozenset(('world', 'leden', 'hidden')),
        'leden': frozenset(('leden', 'hidden'))
    }

    for v in visibility:
        actual |= implies.get(v, frozenset(v))

    return actual

class FotoEntity(SONWrapper):
    CACHES = {}

    def __init__(self, data):
        super(FotoEntity, self).__init__(data, fcol)

    @property
    def id(self):
        return str(self._data['_id'])

    oldId = son_property(('oldId',))
    name = son_property(('name',))
    path = son_property(('path',))
    _type = son_property(('type',))
    caches = son_property(('caches',), ())
    title = son_property(('title',))
    description = son_property(('description',))
    date = son_property(('date',))
    rotation = son_property(('rotation',))
    size = son_property(('size',))

    visibility = son_property(('visibility',))
    _lost = son_property(('lost',))
    effective_visibility = son_property(('effectiveVisibility',))

    @property
    def is_root(self):
        return self.path is None

    def required_visibility(self, user):
        if user is None:
            return frozenset(('world',))
        if is_admin(user):
            return frozenset(('leden', 'world', 'hidden'))
        if 'leden' in user.cached_groups_names:
            return frozenset(('leden', 'world'))
        return frozenset(('world',))

    def _update_effective_visibility(self, parent, save=True, recursive=False):
        '''
        Update the effectiveVisibility property

        `recursive` keyword argument is ignored
        '''
        if self.effective_visibility is not None:
            return False

        if self.is_root:
            # root
            parent_effective_visibility = self.visibility
        else:
            parent_effective_visibility = parent.effective_visibility

        visibilities = actual_visibility(parent_effective_visibility) & \
                       actual_visibility(self.visibility)
        order = ['world', 'leden', 'hidden']+self.visibility
        if visibilities:
            for v in order:
                if v in visibilities:
                    effective_visibility = [v]
                    break
        else:
            effective_visibility = []

        if self._lost:
            effective_visibility = []

        self.effective_visibility = effective_visibility

        if save:
            self.save()

        return True

    def may_view(self, user):
        return bool(self.required_visibility(user)
                        & frozenset(self.effective_visibility))

    @permalink
    def get_browse_url(self):
        return ('fotos-browse', (), {'path': self.full_path})

    @property
    def full_path(self):
        if not self.path:
            return self.name
        return self.path + '/' + self.name

    # NOTE keep up to date with media/fotos.js
    @permalink
    def get_cache_url(self, cache):
        return ('fotos-cache', (), {'path': self.full_path,
                                    'cache': cache})

    @permalink
    def get_thumbnail_url(self):
        return ('fotos-cache', (), {'path': self.full_path,
                                   'cache': 'thumb'})
    @permalink
    def get_thumbnail2x_url(self):
        return ('fotos-cache', (), {'path': self.full_path,
                                   'cache': 'thumb2x'})

    def lock_cache(self, cache):
        ret = lcol.find_and_modify({'_id': self._id},
                                   {'$addToSet': {'cacheLocks': cache}},
                                   upsert=True)
        if ret is None:
            return True
        return cache not in ret.get('cacheLocks', ())

    def unlock_cache(self, cache):
        ret = lcol.update({'_id': self._id},
                          {'$pull': {'cacheLocks': cache}})

    def get_cache_path(self, cache):
        if cache == 'full':
            return os.path.join(settings.PHOTOS_DIR, self.path, self.name)
        path = os.path.join(settings.PHOTOS_CACHE_DIR, cache,
                            self.path, self.name)
        ext = self.CACHES[cache].ext
        if ext:
            path += '.' + ext
        return path

    def get_cache_mimetype(self, cache):
        mimetype = self.CACHES[cache].mimetype
        if mimetype is not None:
            return mimetype
        return mimetypes.guess_type(self.get_cache_path(cache))[0]

    def get_cache_size(self, cache):
        c = self.CACHES[cache]
        if self.rotation in [90, 270]:
            # rotated
            height, width = self.size
        else:
            # normal
            width, height = self.size
        return resize_proportional(width, height, c.maxwidth, c.maxheight)

    def ensure_cached(self, cache):
        if not cache in self.CACHES:
            raise KeyError
        if cache in self.caches or cache == 'full':
            return True
        if not self.lock_cache(cache):
            return False
        try:
            self._cache(cache)
            # Normally, we would just modify _data and .save().  However,
            # as the _cache operation may take quite some time, a full
            # .save() might overwrite other changes. (Like other caches.)
            # Thus we perform the change manually.
            if not 'caches' in self._data:
                self._data['caches'] = []
            self._data['caches'].append(cache)
            fcol.update({'_id': self._id},
                        {'$addToSet': {'caches': cache}})
        finally:
            self.unlock_cache(cache)
        return True

    def _cache(self, cache):
        raise NotImplementedError

    def update_metadata(self, parent, save=True):
        '''
        Load metadata from file if it doesn't exist yet
        '''
        return self._update_effective_visibility(parent, save=save, recursive=False)

    @property
    def original_path(self):
        return os.path.join(settings.PHOTOS_DIR, self.path, self.name)

    @property
    def mongo_path_prefix(self):
        '''
        Return the MongoDB query filter operator to match the path of this
        entry and all children of this element.
        '''
        if self.is_root:
            # root entity
            return {'$exists': True, '$ne': None}
        # non-root entity
        return {'$regex': re.compile(
                            "^%s(/|$)" % re.escape(self.full_path))}

    def get_parent(self):
        if self.is_root:
            return None
        return by_path(self.path)

    def set_title(self, title, save=True):
        self.title = title
        if save:
            self.save()

    def set_description(self, description, save=True):
        self.description = description
        if save:
            self.save()

    def update_visibility(self, visibility):
        '''
        Update the visibility, clear and recalculate effective visibility.
        This object will be saved afterwards.
        '''
        if self.visibility == visibility:
            return

        # First delete all old effective visibilities, in case something goes
        # wrong during the update.
        self._clear_visibility()

        # And now save and recalculate effective visibilities recursively.
        self.visibility = visibility
        self.save()
        self._update_effective_visibility(self.get_parent())

    def _clear_visibility(self):
        self.effective_visibility = None
        fcol.update({'path': self.mongo_path_prefix},
                    {'$unset': {'effectiveVisibility': ''}},
                    multi=True)

    @property
    def is_lost(self):
        return bool(self._lost)

    def lost(self, parent):
        if self._lost:
            return

        self._clear_visibility()
        self._lost = True
        self.save()
        self._update_effective_visibility(parent)

    def found(self, parent):
        if not self._lost:
            return

        self._clear_visibility()
        self._lost = False
        self.save()
        self._update_effective_visibility(parent)

    def get_tags(self):
        '''
        Return all tags of this entity (as User object)
        '''
        if 'tags' not in self._data:
            return None

        idmap = Es.by_ids(self._data['tags'])
        people = []
        for id in self._data['tags']:
            people.append(idmap[id])

        return people

    def set_tags(self, tags, save=True):
        '''
        Set tags by their usernames.
        '''
        name2id = Es.ids_by_names(tags)
        self._data['tags'] = []
        for name in tags:
            self._data['tags'].append(name2id[name])

        if save:
            self.save()

class FotoAlbum(FotoEntity):
    def __init__(self, data):
        super(FotoAlbum, self).__init__(data)

    def list(self, user):
        required_visibility = self.required_visibility(user)
        albums = map(entity, fcol.find({'path': self.full_path,
                           'type': 'album',
                           'effectiveVisibility': {'$in': tuple(required_visibility)}},
                           ).sort([('date', -1), ('name', 1)]))
        fotos = map(entity, fcol.find({'path': self.full_path,
                           'type': {'$ne': 'album'},
                           'effectiveVisibility': {'$in': tuple(required_visibility)}},
                           ).sort([('date', 1), ('name', 1)]))

        return albums+fotos

    def list_all(self):
        '''
        Return all children regardless of visibility.
        '''
        return map(entity, fcol.find({'path': self.full_path}).sort('name', 1))

    def get_random_foto_for(self, user):
        r = random.random()
        required_visibility = self.required_visibility(user)
        while True:
            f = entity(fcol.find_one(
                    {'random': {'$lt': r},
                     'path': {'$regex': re.compile(
                                "^%s(/|$)" % re.escape(self.full_path))},
                     'type': 'foto',
                     'effectiveVisibility': {'$in': tuple(required_visibility)}},
                        sort=[('random',-1)]))
            if f is not None:
                return f
            if r == 1:
                return None
            r = 1

    def search(self, q, user):
        required_visibility = self.required_visibility(user)
        query_filter = {'path': self.mongo_path_prefix,
                        'effectiveVisibility': {'$in': tuple(required_visibility)}}

        if q.startswith('album:'):
            album = q[len('album:'):]
            query_filter['type'] = 'album'
            query_filter = {'$and': [
                query_filter,
                {'$or': [
                    {'name': {'$regex': re.compile(re.escape(album),
                                                   re.IGNORECASE)}},
                    {'title': {'$regex': re.compile(re.escape(album),
                                                    re.IGNORECASE)}}
                ]}]}

        elif q.startswith('tag:'):
            user_id = Es.id_by_name(q[len('tag:'):])
            if user_id is None:
                return
            query_filter['type'] = {'$ne': 'album'}
            query_filter['tags'] = user_id

        else:
            # Do a full-text search.
            # Because full-text search can't be combined with other query
            # operations in MongoDB 2.4, we join and sort them by hand. MongoDB
            # 2.6+ has this feature.

            # Text search (name and description)
            results = map(lambda r: r['obj'],
                db.command('text', 'fotos',
                        search=q,
                        filter=query_filter,
                        limit=96, # dividable by 2, 3 and 4
                      )['results'])

            # Tag search
            words = split_words(q.lower())
            users = []
            words_set = set(words)
            for user in Es.users():
                if user.first_name is None:
                    # One (test) entity doesn't have a first_name
                    continue
                if user.first_name.lower() in words_set or user.last_name.lower() in words_set:
                    users.append(_id(user))
            for r in fcol.find({'$and': [query_filter,
                                         {'tags': {'$in': users}}]}):
                results.append(r)

            # The sort key does some magic.
            # The first element is to sort on type first: albums go first, then
            # other types (photos): True > False in Python
            # The second is just a sort on date.
            # We want to sort on date, but have the albums first in the list.
            results.sort(key=lambda r: (r['type'] == 'album', r['date']), reverse=True)

            # Remove duplicates.
            # Note that this has a very small race condition: if the date or
            # type is changed between the two queries above, the _id may not be
            # sorted the same. This shouldn't occur in practice.
            previous = None
            for r in results:
                if previous and previous['_id'] == r['_id']:
                    continue
                yield entity(r)
                previous = r

            return

        # search for album or tag
        for o in fcol.find(query_filter).sort('date', -1):
            yield entity(o)

    def update_metadata(self, parent, save=True):
        updated = super(FotoAlbum, self).update_metadata(parent, save=False)

        if self.date is not None:
            if save and updated:
                self.save()
            return updated

        self.date = settings.DT_MIN # NULL date/time
        try:
            self.date = datetime.datetime.strptime(self.name[:10], '%Y-%m-%d')
        except ValueError:
            # there is no date
            pass

        if save:
            self.save()
        return True

    def _update_effective_visibility(self, parent, save=True, recursive=True):
        if recursive and not save:
            raise ValueError('recursion without save is not recommended')

        if not super(FotoAlbum, self)._update_effective_visibility(parent, save=False):
            # effective visibility did not change, so children won't change too
            return False

        if recursive:
            for foto in self.list_all():
                updated = foto._update_effective_visibility(self, save=save) or updated

        if save:
            self.save()

        return True

class Foto(FotoEntity):
    # keep up to date with fotos.js (onresize)
    CACHES = {'thumb': cache_tuple('jpg', 'image/jpeg', 200, 200, 85),
              'thumb2x': cache_tuple('jpg', 'image/jpeg', 400, 400, 85),
              'large': cache_tuple('jpg', 'image/jpeg', 850, 850, 90),
              'large2x': cache_tuple('jpg', 'image/jpeg', 1700, 1700, 90),
              'full': cache_tuple(None, None, None, None, None),
              }


    def __init__(self, data):
        super(Foto, self).__init__(data)

    def update_metadata(self, parent, save=True):
        '''
        Load EXIF metadata from file if it hasn't been loaded yet.
        '''
        updated = super(Foto, self).update_metadata(parent, save=False)

        if self._lost:
            if save and updated:
                self.save()
            return updated

        if None not in [self.rotation, self.date, self.size]:
            if save and updated:
                self.save()
            return updated

        img = Image.open(self.original_path)
        exif = {}
        if hasattr(img, '_getexif'):
            exifData = img._getexif()
            if exifData is not None:
                for k, v in exifData.items():
                    if k not in TAGS:
                        continue
                    exif[TAGS[k]] = v

        if self.rotation is None:
            self.rotation = 0
            try:
                orientation = int(exif.get('Orientation', '1'))
                if orientation == 1:
                    self.rotation = 0
                elif orientation == 3:
                    self.rotation = 180
                elif orientation == 6:
                    self.rotation = 90
                elif orientation == 8:
                    self.rotation = 270
                # other rotations are mirrored, and won't occur much in practice
            except ValueError:
                # Rotation cannot be converted to an int (invalid input),
                # ignore.
                pass

        if self.date is None:
            self.date = settings.DT_MIN # NULL date/time
            if 'DateTimeOriginal' in exif:
                try:
                    self.date = datetime.datetime.strptime(exif['DateTimeOriginal'], '%Y:%m:%d %H:%M:%S')
                except ValueError:
                    # Ignore: the timestamp did not match the format.
                    # For example, the year might be below year 1 (a zero
                    # timestamp).
                    pass
                except TypeError:
                    # The string might contain crazy stuff like NULL bytes.
                    pass

        if self.size is None:
            self.size = img.size

        if save:
            self.save()

        return True

    def set_rotation(self, rotation, save=True):
        if rotation == self.rotation:
            return

        self.rotation = rotation
        self.caches = [] # invalidate cache
        if save:
            self.save()

    def _cache(self, cache):
        if cache == 'full':
            return
        source = self.original_path
        target = self.get_cache_path(cache)
        target_dir = os.path.dirname(target)
        if not os.path.exists(target_dir):
            os.makedirs(target_dir)

        size = '%dx%d' % self.get_cache_size(cache)
        subprocess.check_call(['convert',
                         source,
                         '-strip',
                         '-rotate', str(self.rotation),
                         '-resize', size,
                         '-interlace', 'Plane',
                         '-quality', str(self.CACHES[cache].quality),
                         target])


class Video(FotoEntity):
    def __init__(self, data):
        super(Video, self).__init__(data)

def all():
    for d in fcol.find():
        yield entity(d)

CACHE_TYPES = (
        'thumb',
        'thumb2x',
        'large',
        'large2x',
        'full',
    )

TYPE_MAP = {
        'album':        FotoAlbum,
        'foto':         Foto,
        'video':        Video
    }

# vim: et:sta:bs=2:sw=4:
