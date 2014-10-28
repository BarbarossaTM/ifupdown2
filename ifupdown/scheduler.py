#!/usr/bin/python
#
# Copyright 2014 Cumulus Networks, Inc. All rights reserved.
# Author: Roopa Prabhu, roopa@cumulusnetworks.com
#
# ifaceScheduler --
#    interface scheduler
#

from statemanager import *
from iface import *
from graph import *
from collections import deque
from collections import OrderedDict
import logging
import traceback
import sys
from graph import *
from collections import deque
from threading import *
from ifupdownbase import *

class ifaceSchedulerFlags():
    """ Enumerates scheduler flags """

    INORDER = 0x1
    POSTORDER = 0x2

class ifaceScheduler():
    """ scheduler functions to schedule configuration of interfaces.

    supports scheduling of interfaces serially in plain interface list
    or dependency graph format.

    """

    _STATE_CHECK = True

    _SCHED_RETVAL = True

    @classmethod
    def run_iface_op(cls, ifupdownobj, ifaceobj, op, cenv=None):
        """ Runs sub operation on an interface """
        ifacename = ifaceobj.name

        if (cls._STATE_CHECK and
            (ifaceobj.state >= ifaceState.from_str(op))):
            ifupdownobj.logger.debug('%s: already in state %s' %(ifacename, op))
            return
        if not ifupdownobj.ADDONS_ENABLE: return
        if op == 'query-checkcurr':
            query_ifaceobj=ifupdownobj.create_n_save_ifaceobjcurr(ifaceobj)
        for mname in ifupdownobj.module_ops.get(op):
            m = ifupdownobj.modules.get(mname)
            err = 0
            try:
                if hasattr(m, 'run'):
                    msg = ('%s: %s : running module %s' %(ifacename, op, mname))
                    if op == 'query-checkcurr':
                        # Dont check curr if the interface object was 
                        # auto generated
                        if (ifaceobj.priv_flags & ifupdownobj.NOCONFIG):
                            continue
                        ifupdownobj.logger.debug(msg)
                        m.run(ifaceobj, op, query_ifaceobj,
                              ifaceobj_getfunc=ifupdownobj.get_ifaceobjs)
                    else:
                        ifupdownobj.logger.debug(msg)
                        m.run(ifaceobj, op, ifaceobj_getfunc=ifupdownobj.get_ifaceobjs)
            except Exception, e:
                err = 1
                ifupdownobj.log_error(str(e))
                err = 0  # error can be ignored by log_error, in which case
                         # reset err flag
            finally:
                if err or ifaceobj.status == ifaceStatus.ERROR:
                    ifaceobj.set_state_n_status(ifaceState.from_str(op),
                                                ifaceStatus.ERROR)
                    if 'up' in  op or 'down' in op:
                        cls._SCHED_RETVAL = False
                else:
                    ifaceobj.set_state_n_status(ifaceState.from_str(op),
                                                ifaceStatus.SUCCESS)

        if ifupdownobj.config.get('addon_scripts_support', '0') == '1':
            # execute /etc/network/ scripts 
            for mname in ifupdownobj.script_ops.get(op, []):
                ifupdownobj.logger.debug('%s: %s : running script %s'
                    %(ifacename, op, mname))
                try:
                    ifupdownobj.exec_command(mname, cmdenv=cenv)
                except Exception, e:
                    ifupdownobj.log_error(str(e))

    @classmethod
    def run_iface_list_ops(cls, ifupdownobj, ifaceobjs, ops):
        """ Runs all operations on a list of interface
            configurations for the same interface
        """
        # minor optimization. If operation is 'down', proceed only
        # if interface exists in the system
        ifacename = ifaceobjs[0].name
        ifupdownobj.logger.info('%s: running ops ...' %ifacename)
        if ('down' in ops[0] and ifaceobjs[0].type != ifaceType.BRIDGE_VLAN and
                not ifupdownobj.link_exists(ifacename)):
            ifupdownobj.logger.debug('%s: does not exist' %ifacename)
            # run posthook before you get out of here, so that
            # appropriate cleanup is done
            posthookfunc = ifupdownobj.sched_hooks.get('posthook')
            if posthookfunc:
                for ifaceobj in ifaceobjs:
                    ifaceobj.status = ifaceStatus.SUCCESS
                    posthookfunc(ifupdownobj, ifaceobj, 'down')
            return 
        for op in ops:
            # first run ifupdownobj handlers. This is good enough
            # for the first object in the list
            handler = ifupdownobj.ops_handlers.get(op)
            if handler:
                if (not ifaceobjs[0].addr_method or
                    (ifaceobjs[0].addr_method and
                    ifaceobjs[0].addr_method != 'manual')):
                    handler(ifupdownobj, ifaceobjs[0])
            for ifaceobj in ifaceobjs:
                cls.run_iface_op(ifupdownobj, ifaceobj, op,
                    cenv=ifupdownobj.generate_running_env(ifaceobj, op)
                        if ifupdownobj.config.get('addon_scripts_support',
                            '0') == '1' else None)
        posthookfunc = ifupdownobj.sched_hooks.get('posthook')
        if posthookfunc:
            [posthookfunc(ifupdownobj, ifaceobj, ops[0])
                for ifaceobj in ifaceobjs]

    @classmethod
    def _check_upperifaces(cls, ifupdownobj, ifaceobj, ops, parent,
                           followdependents=False):
        """ Check if upperifaces are hanging off us and help caller decide
        if he can proceed with the ops on this device

        Returns True or False indicating the caller to proceed with the
        operation.
        """
        # proceed only for down operation
        if 'down' not in ops[0]:
            return True

        if (ifupdownobj.FORCE or
                not ifupdownobj.ADDONS_ENABLE or
                (not ifupdownobj.is_ifaceobj_noconfig(ifaceobj) and
                ifupdownobj.config.get('warn_on_ifdown', '0') == '0' and
                not ifupdownobj.ALL)):
            return True

        if ifaceobj.type == ifaceType.BRIDGE:
            #
            # XXX: This function's job is to return True for
            # logical devices when any of the upperifaces are still around.
            # In the new model, where bridge is represented as a dependency
            # for a bridge port, bridge becomes a lowerdevice of a bridge port,
            # which is not really true. To handle this case, add a special
            # check for bridge device. Will figure out a better way to handle
            # this.
            # Long term this function should be replaced by
            # walking the sysfs links to upper and lower device available in
            # latest kernels.
            try:
                if os.listdir('/sys/class/net/%s/brif' %ifaceobj.name):
                    return False
                else:
                    return True
            except Exception:
                pass
                return True

        ulist = ifaceobj.upperifaces
        if not ulist:
            return True

        # Get the list of upper ifaces other than the parent
        tmpulist = ([u for u in ulist if u != parent] if parent
                    else ulist)
        if not tmpulist:
            return True
        # XXX: This is expensive. Find a cheaper way to do this.
        # if any of the upperdevs are present,
        # return false to the caller to skip this interface
        for u in tmpulist:
            if ifupdownobj.link_exists(u):
                if not ifupdownobj.ALL:
                    if ifupdownobj.is_ifaceobj_noconfig(ifaceobj):
                        ifupdownobj.logger.info('%s: skipping interface down,'
                            %ifaceobj.name + ' upperiface %s still around ' %u)
                    else:
                        ifupdownobj.logger.warn('%s: skipping interface down,'
                            %ifaceobj.name + ' upperiface %s still around ' %u)
                return False
        return True

    @classmethod
    def run_iface_graph(cls, ifupdownobj, ifacename, ops, parent=None,
                        order=ifaceSchedulerFlags.POSTORDER,
                        followdependents=True):
        """ runs interface by traversing all nodes rooted at itself """

        # Each ifacename can have a list of iface objects
        ifaceobjs = ifupdownobj.get_ifaceobjs(ifacename)
        if not ifaceobjs:
            raise Exception('%s: not found' %ifacename)

        for ifaceobj in ifaceobjs:
            if not cls._check_upperifaces(ifupdownobj, ifaceobj,
                                          ops, parent, followdependents):
                return

        # If inorder, run the iface first and then its dependents
        if order == ifaceSchedulerFlags.INORDER:
            cls.run_iface_list_ops(ifupdownobj, ifaceobjs, ops)

        for ifaceobj in ifaceobjs:
            # Run lowerifaces or dependents
            dlist = ifaceobj.lowerifaces
            if dlist:
                ifupdownobj.logger.debug('%s: found dependents %s'
                            %(ifacename, str(dlist)))
                try:
                    if not followdependents:
                        # XXX: this is yet another extra step,
                        # but is needed for interfaces that are
                        # implicit dependents. even though we are asked to
                        # not follow dependents, we must follow the ones
                        # that dont have user given config. Because we own them
                        new_dlist = [d for d in dlist
                                    if ifupdownobj.is_iface_noconfig(d)]
                        if new_dlist:
                            cls.run_iface_list(ifupdownobj, new_dlist, ops,
                                           ifacename, order, followdependents,
                                           continueonfailure=False)
                    else:
                        cls.run_iface_list(ifupdownobj, dlist, ops,
                                            ifacename, order,
                                            followdependents,
                                            continueonfailure=False)
                except Exception, e:
                    if (ifupdownobj.ignore_error(str(e))):
                        pass
                    else:
                        # Dont bring the iface up if children did not come up
                        ifaceobj.set_state_n_status(ifaceState.NEW,
                                                ifaceStatus.ERROR)
                        raise
        if order == ifaceSchedulerFlags.POSTORDER:
            cls.run_iface_list_ops(ifupdownobj, ifaceobjs, ops)

    @classmethod
    def run_iface_list(cls, ifupdownobj, ifacenames,
                       ops, parent=None, order=ifaceSchedulerFlags.POSTORDER,
                       followdependents=True, continueonfailure=True):
        """ Runs interface list """

        for ifacename in ifacenames:
            try:
              cls.run_iface_graph(ifupdownobj, ifacename, ops, parent,
                      order, followdependents)
            except Exception, e:
                if continueonfailure:
                    if ifupdownobj.logger.isEnabledFor(logging.DEBUG):
                        traceback.print_tb(sys.exc_info()[2])
                    ifupdownobj.logger.error('%s : %s' %(ifacename, str(e)))
                    pass
                else:
                    if (ifupdownobj.ignore_error(str(e))):
                        pass
                    else:
                        raise Exception('%s : (%s)' %(ifacename, str(e)))

    @classmethod
    def run_iface_graph_upper(cls, ifupdownobj, ifacename, ops, parent=None,
                        followdependents=True, skip_root=False):
        """ runs interface by traversing all nodes rooted at itself """

        # Each ifacename can have a list of iface objects
        ifaceobjs = ifupdownobj.get_ifaceobjs(ifacename)
        if not ifaceobjs:
            raise Exception('%s: not found' %ifacename)

        if not skip_root:
            # run the iface first and then its upperifaces
            cls.run_iface_list_ops(ifupdownobj, ifaceobjs, ops)
        for ifaceobj in ifaceobjs:
            # Run upperifaces
            ulist = ifaceobj.upperifaces
            if ulist:
                ifupdownobj.logger.debug('%s: found upperifaces %s'
                                            %(ifacename, str(ulist)))
                try:
                    cls.run_iface_list_upper(ifupdownobj, ulist, ops,
                                            ifacename,
                                            followdependents,
                                            continueonfailure=True)
                except Exception, e:
                    if (ifupdownobj.ignore_error(str(e))):
                        pass
                    else:
                        raise

    @classmethod
    def run_iface_list_upper(cls, ifupdownobj, ifacenames,
                       ops, parent=None, followdependents=True,
                       continueonfailure=True, skip_root=False):
        """ Runs interface list """

        for ifacename in ifacenames:
            try:
              cls.run_iface_graph_upper(ifupdownobj, ifacename, ops, parent,
                      followdependents, skip_root)
            except Exception, e:
                if ifupdownobj.logger.isEnabledFor(logging.DEBUG):
                    traceback.print_tb(sys.exc_info()[2])
                ifupdownobj.logger.warn('%s : %s' %(ifacename, str(e)))
                pass

    @classmethod
    def get_sorted_iface_list(cls, ifupdownobj, ifacenames, ops,
                              dependency_graph, indegrees=None):
        if len(ifacenames) == 1:
            return ifacenames
        # Get a sorted list of all interfaces
        if not indegrees:
            indegrees = OrderedDict()
            for ifacename in dependency_graph.keys():
                indegrees[ifacename] = ifupdownobj.get_iface_refcnt(ifacename)
        ifacenames_all_sorted = graph.topological_sort_graphs_all(
                                        dependency_graph, indegrees)
        # if ALL was set, return all interfaces
        if ifupdownobj.ALL:
            return ifacenames_all_sorted

        # else return ifacenames passed as argument in sorted order
        ifacenames_sorted = []
        [ifacenames_sorted.append(ifacename)
                        for ifacename in ifacenames_all_sorted
                            if ifacename in ifacenames]
        return ifacenames_sorted

    @classmethod
    def sched_ifaces(cls, ifupdownobj, ifacenames, ops,
                dependency_graph=None, indegrees=None,
                order=ifaceSchedulerFlags.POSTORDER,
                followdependents=True):
        """ runs interface configuration modules on interfaces passed as
            argument. Runs topological sort on interface dependency graph.

        Args:
            **ifupdownobj** (object): ifupdownMain object 

            **ifacenames** (list): list of interface names

            **ops** : list of operations to perform eg ['pre-up', 'up', 'post-up']

            **dependency_graph** (dict): dependency graph in adjacency list format

        Kwargs:
            **indegrees** (dict): indegree array of the dependency graph

            **order** (int): ifaceSchedulerFlags (POSTORDER, INORDER)

            **followdependents** (bool): follow dependent interfaces if true

        """
        #
        # Algo:
        # if ALL/auto interfaces are specified,
        #   - walk the dependency tree in postorder or inorder depending
        #     on the operation.
        #     (This is to run interfaces correctly in order)
        # else:
        #   - sort iface list if the ifaces belong to a "class"
        #   - else just run iface list in the order they were specified
        #
        # Run any upperifaces if available
        #
        followupperifaces = False
        run_queue = []
        skip_ifacesort = int(ifupdownobj.config.get('skip_ifacesort', '0'))
        if not skip_ifacesort and not indegrees:
            indegrees = OrderedDict()
            for ifacename in dependency_graph.keys():
                indegrees[ifacename] = ifupdownobj.get_iface_refcnt(ifacename)

        if not ifupdownobj.ALL:
            # If there is any interface that does exist, maybe it is a
            # logical interface and we have to followupperifaces when it
            # comes up, so get that list.
            if any([True for i in ifacenames
                    if ifupdownobj.must_follow_upperifaces(i)]):
                followupperifaces = (True if
                                    [i for i in ifacenames
                                        if not ifupdownobj.link_exists(i)]
                                        else False)
            if not skip_ifacesort and ifupdownobj.IFACE_CLASS:
                # sort interfaces only if allow class was specified and
                # not skip_ifacesort
                run_queue = cls.get_sorted_iface_list(ifupdownobj, ifacenames,
                                    ops, dependency_graph, indegrees)
                if run_queue and 'up' in ops[0]:
                    run_queue.reverse()
        else:
            # if -a is set, we dont really have to sort. We pick the interfaces
            # that have no parents and 
            if not skip_ifacesort:
                sorted_ifacenames = cls.get_sorted_iface_list(ifupdownobj,
                                            ifacenames, ops, dependency_graph,
                                            indegrees)
                if sorted_ifacenames:
                    # pick interfaces that user asked
                    # and those that dont have any dependents first
                    [run_queue.append(ifacename)
                        for ifacename in sorted_ifacenames
                            if ifacename in ifacenames and
                            not indegrees.get(ifacename)]
                    ifupdownobj.logger.debug('graph roots (interfaces that ' +
                            'dont have dependents):' + ' %s' %str(run_queue))
                else:
                    ifupdownobj.logger.warn('interface sort returned None')

        # If queue not present, just run interfaces that were asked by the user
        if not run_queue:
            run_queue = list(ifacenames)
            if 'down' in ops[0]:
                run_queue.reverse()

        # run interface list
        cls.run_iface_list(ifupdownobj, run_queue, ops,
                           parent=None, order=order,
                           followdependents=followdependents)
        if not cls._SCHED_RETVAL:
            raise Exception()

        if (ifupdownobj.config.get('skip_upperifaces', '0') == '0' and
                ((not ifupdownobj.ALL and followdependents) or
                followupperifaces) and
                'up' in ops[0]):
            # If user had given a set of interfaces to bring up
            # try and execute 'up' on the upperifaces
            ifupdownobj.logger.info('running upperifaces (parent interfaces) ' +
                                    'if available ..')
            cls._STATE_CHECK = False
            cls.run_iface_list_upper(ifupdownobj, ifacenames, ops,
                                     skip_root=True)
            cls._STATE_CHECK = True
