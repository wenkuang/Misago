import urlparse
from mptt.managers import TreeManager
from mptt.models import MPTTModel, TreeForeignKey
from django.conf import settings
from django.core.cache import cache
from django.core.urlresolvers import reverse
from django.db import models
from django.db.models import Sum
from django.utils.translation import ugettext_lazy as _
from misago.signals import delete_forum_content, move_forum_content, rename_forum, rename_user
from misago.thread import local

_thread_local = local()

class ForumManager(TreeManager):
    @property
    def forums_tree(self):
        try:
            return _thread_local.misago_forums_tree
        except AttributeError:
            _thread_local.misago_forums_tree = None
        return _thread_local.misago_forums_tree

    @forums_tree.setter
    def forums_tree(self, value):
        _thread_local.misago_forums_tree = value

    def special_pk(self, name):
        self.populate_tree()
        return self.forums_tree.get(name).pk

    def special_model(self, name):
        self.populate_tree()
        return self.forums_tree.get(name)

    def populate_tree(self, force=False):
        if not self.forums_tree:
            self.forums_tree = cache.get('forums_tree', 'nada')
        if self.forums_tree == 'nada' or force:
            self.forums_tree = {}
            for forum in Forum.objects.order_by('lft'):
                self.forums_tree[forum.pk] = forum
                if forum.special:
                    self.forums_tree[forum.special] = forum
            cache.set('forums_tree', self.forums_tree)

    def forum_parents(self, forum, include_self=False):
        self.populate_tree()
        parents = []
        parent = self.forums_tree[forum]
        if include_self:
            parents.append(parent)
        while parent.level > 1:
            parent = self.forums_tree[parent.parent_id]
            parents.append(parent)
        result = []
        for i in reversed(parents):
            result.append(i)
        return list(result)

    def parents_aware_forum(self, forum):
        self.populate_tree()
        proxy = Forum()
        try:
            proxy.id = forum.pk
            proxy.pk = forum.pk
        except AttributeError:
            proxy.id = forum
            proxy.pk = forum
        proxy.closed = False
        for parent in self.forum_parents(proxy.pk):
            if parent.closed:
                proxy.closed = True
                return proxy
        return proxy

    def treelist(self, acl, parent=None, tracker=None):
        complete_list = []
        forums_list = []
        parents = {}

        if parent:
            queryset = Forum.objects.filter(pk__in=acl.known_forums).filter(lft__gt=parent.lft).filter(rght__lt=parent.rght).order_by('lft')
        else:
            queryset = Forum.objects.filter(pk__in=acl.known_forums).order_by('lft')

        for forum in queryset.iterator():
            forum.subforums = []
            forum.is_read = False
            if tracker:
                forum.is_read = tracker.is_read(forum)
            parents[forum.pk] = forum
            complete_list.append(forum)
            if forum.parent_id in parents:
                parents[forum.parent_id].subforums.append(forum)
            else:
                forums_list.append(forum)

        # Second iteration - sum up forum counters
        for forum in reversed(complete_list):
            if forum.parent_id in parents and parents[forum.parent_id].type != 'redirect':
                parents[forum.parent_id].threads += forum.threads
                parents[forum.parent_id].posts += forum.posts
                if acl.can_browse(forum.pk):
                    # If forum is unread, make parent unread too
                    if not forum.is_read:
                        parents[forum.parent_id].is_read = False
                    # Sum stats
                    if forum.last_thread_date and (not parents[forum.parent_id].last_thread_date or forum.last_thread_date > parents[forum.parent_id].last_thread_date):
                        parents[forum.parent_id].last_thread_id = forum.last_thread_id
                        parents[forum.parent_id].last_thread_name = forum.last_thread_name
                        parents[forum.parent_id].last_thread_slug = forum.last_thread_slug
                        parents[forum.parent_id].last_thread_date = forum.last_thread_date
                        parents[forum.parent_id].last_poster_id = forum.last_poster_id
                        parents[forum.parent_id].last_poster_name = forum.last_poster_name
                        parents[forum.parent_id].last_poster_slug = forum.last_poster_slug
                        parents[forum.parent_id].last_poster_style = forum.last_poster_style
        return forums_list

    def ignored_users(self, user, forums):
        check_ids = []
        for forum in forums:
            forum.last_poster_ignored = False
            if user.is_authenticated() and user.pk != forum.last_poster_id and forum.last_poster_id and not forum.last_poster_id in check_ids:
                check_ids.append(forum.last_poster_id)
        ignored_ids = []
        if check_ids and user.is_authenticated():
            for user in user.ignores.filter(id__in=check_ids).values('id'):
                ignored_ids.append(user['id'])

    def readable_forums(self, acl, include_special=False):
        self.populate_tree()
        readable = []
        for pk, forum in self.forums_tree.items():
            if ((include_special or not forum.special) and
                    acl.forums.can_browse(forum.pk) and
                    acl.threads.acl[forum.pk]['can_read_threads'] == 2):
                readable.append(forum.pk)
        return readable

    def starter_readable_forums(self, acl):
        self.populate_tree()
        readable = []
        for pk, forum in self.forums_tree.items():
            if (not forum.special and
                    acl.forums.can_browse(forum.pk) and
                    acl.threads.acl[forum.pk]['can_read_threads'] == 1):
                readable.append(forum.pk)
        return readable


    def forum_by_name(self, forum, acl):
        forums = self.readable_forums(acl, True)
        forum = forum.lower()
        for f in forums:
            f = self.forums_tree[f]
            if forum == unicode(f).lower():
                return f
        forum_len = len(forum)
        for f in forums:
            f = self.forums_tree[f]
            name = unicode(f).lower()
            if forum == unicode(f).lower()[0:forum_len]:
                return f
        return None


class Forum(MPTTModel):
    parent = TreeForeignKey('self', null=True, blank=True, related_name='children')
    type = models.CharField(max_length=12)
    special = models.CharField(max_length=255, null=True, blank=True)
    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    description = models.TextField(null=True, blank=True)
    description_preparsed = models.TextField(null=True, blank=True)
    threads = models.PositiveIntegerField(default=0)
    threads_delta = models.PositiveIntegerField(default=0)
    posts = models.PositiveIntegerField(default=0)
    posts_delta = models.IntegerField(default=0)
    redirects = models.PositiveIntegerField(default=0)
    redirects_delta = models.IntegerField(default=0)
    last_thread = models.ForeignKey('Thread', related_name='+', null=True, blank=True, on_delete=models.SET_NULL)
    last_thread_name = models.CharField(max_length=255, null=True, blank=True)
    last_thread_slug = models.SlugField(max_length=255, null=True, blank=True)
    last_thread_date = models.DateTimeField(null=True, blank=True)
    last_poster = models.ForeignKey('User', related_name='+', null=True, blank=True, on_delete=models.SET_NULL)
    last_poster_name = models.CharField(max_length=255, null=True, blank=True)
    last_poster_slug = models.SlugField(max_length=255, null=True, blank=True)
    last_poster_style = models.CharField(max_length=255, null=True, blank=True)
    prune_start = models.PositiveIntegerField(default=0)
    prune_last = models.PositiveIntegerField(default=0)
    pruned_archive = models.ForeignKey('self', related_name='+', null=True, blank=True, on_delete=models.SET_NULL)
    redirect = models.CharField(max_length=255, null=True, blank=True)
    attrs = models.CharField(max_length=255, null=True, blank=True)
    show_details = models.BooleanField(default=True)
    style = models.CharField(max_length=255, null=True, blank=True)
    closed = models.BooleanField(default=False)

    objects = ForumManager()

    class Meta:
        app_label = 'misago'

    def save(self, *args, **kwargs):
        super(Forum, self).save(*args, **kwargs)
        cache.delete('forums_tree')

    def delete(self, *args, **kwargs):
        delete_forum_content.send(sender=self)
        super(Forum, self).delete(*args, **kwargs)
        cache.delete('forums_tree')

    def __unicode__(self):
        if self.special == 'private_threads':
           return unicode(_('Private Threads'))
        if self.special == 'reports':
           return unicode(_('Reports'))
        if self.special == 'root':
           return unicode(_('Root Category'))
        return unicode(self.name)

    @property
    def url(self):
        if self.special == 'private_threads':
           reverse('private_threads')
        if self.special == 'reports':
           reverse('reports')
        if self.type == 'category':
            return reverse('category', kwargs={'forum': self.pk, 'slug': self.slug})
        if self.type == 'redirect':
            return reverse('redirect', kwargs={'forum': self.pk, 'slug': self.slug})
        return reverse('forum', kwargs={'forum': self.pk, 'slug': self.slug})

    def thread_link(self, extra):
        if self.special == 'private_threads':
           route_prefix = 'private_thread'
        if self.special == 'reports':
           route_prefix = 'report'
        else:
            route_prefix = 'thread'
        if extra:
            return '%s_%s' % (route_prefix, extra) if extra else route_prefix
        return route_prefix

    def thread_url(self, thread, route=None):
        route_prefix = 'thread'
        if self.special:
            route_prefix = self.special[0:-1]
        link = '%s_%s' % (route_prefix, route) if route else route_prefix
        return reverse(link, kwargs={'thread': thread.pk, 'slug': thread.slug})

    def set_description(self, description):
        self.description = description.strip()
        self.description_preparsed = ''
        if self.description:
            import markdown
            self.description_preparsed = markdown.markdown(description, safe_mode='escape', output_format=settings.OUTPUT_FORMAT)

    def copy_permissions(self, target):
        if target.pk != self.pk:
            from misago.models import Role
            for role in Role.objects.all():
                perms = role.permissions
                try:
                    perms['forums'][self.pk] = perms['forums'][target.pk]
                    role.permissions = perms
                    role.save(force_update=True)
                except KeyError:
                    pass

    def move_content(self, target):
        move_forum_content.send(sender=self, move_to=target)

    def sync_name(self):
        rename_forum.send(sender=self)

    def attr(self, att):
        if self.attrs:
            return att in self.attrs.split()
        return False

    def redirect_domain(self):
        hostname = urlparse.urlparse(self.redirect).hostname
        scheme = urlparse.urlparse(self.redirect).scheme
        if scheme:
            scheme = '%s://' % scheme
        return '%s%s' % (scheme, hostname)

    def new_last_thread(self, thread):
        self.last_thread = thread
        self.last_thread_name = thread.name
        self.last_thread_slug = thread.slug
        self.last_thread_date = thread.last
        self.last_poster = thread.last_poster
        self.last_poster_name = thread.last_poster_name
        self.last_poster_slug = thread.last_poster_slug
        self.last_poster_style = thread.last_poster_style

    def sync_last(self):
        self.last_poster = None
        self.last_poster_name = None
        self.last_poster_slug = None
        self.last_poster_style = None
        self.last_thread = None
        self.last_thread_date = None
        self.last_thread_name = None
        self.last_thread_slug = None
        try:
            last_thread = self.thread_set.filter(moderated=False).filter(deleted=False).order_by('-last').all()[:1][0]
            self.last_poster_name = last_thread.last_poster_name
            self.last_poster_slug = last_thread.last_poster_slug
            self.last_poster_style = last_thread.last_poster_style
            if last_thread.last_poster:
                self.last_poster = last_thread.last_poster
            self.last_thread = last_thread
            self.last_thread_date = last_thread.last
            self.last_thread_name = last_thread.name
            self.last_thread_slug = last_thread.slug
        except (IndexError, AttributeError):
            pass

    def sync(self):
        threads_qs = self.thread_set.filter(moderated=False).filter(deleted=False)
        self.posts = self.threads = threads_qs.count()
        replies = threads_qs.aggregate(Sum('replies'))
        if replies['replies__sum']:
            self.posts += replies['replies__sum']
        self.sync_last()

    def prune(self):
        pass


"""
Signals
"""
def rename_user_handler(sender, **kwargs):
    Forum.objects.filter(last_poster=sender).update(
                                                    last_poster_name=sender.username,
                                                    last_poster_slug=sender.username_slug,
                                                    )

rename_user.connect(rename_user_handler, dispatch_uid='rename_forums_last_poster')