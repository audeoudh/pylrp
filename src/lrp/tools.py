# Copyright Laboratoire d'Informatique de Grenoble (2017)
#
# This file is part of pylrp.
#
# Pylrp is a Python/Linux implementation of the LRP routing protocol.
#
# This software is governed by the CeCILL license under French law and
# abiding by the rules of distribution of free software.  You can  use,
# modify and/ or redistribute the software under the terms of the CeCILL
# license as circulated by CEA, CNRS and INRIA at the following URL
# "http://www.cecill.info".
#
# As a counterpart to the access to the source code and  rights to copy,
# modify and redistribute granted by the license, users are provided only
# with a limited warranty  and the software's author,  the holder of the
# economic rights,  and the successive licensors  have only  limited
# liability.
#
# In this respect, the user's attention is drawn to the risks associated
# with loading,  using,  modifying and/or developing or reproducing the
# software by the user in light of its specific status of free software,
# that may mean  that it is complicated to manipulate,  and  that  also
# therefore means  that it is reserved for developers  and  experienced
# professionals having in-depth computer knowledge. Users are therefore
# encouraged to load and test the software's suitability as regards their
# requirements in conditions enabling the security of their systems and/or
# data to be ensured and,  more generally, to use and operate it in the
# same conditions as regards security.
#
# The fact that you are presently reading this means that you have had
# knowledge of the CeCILL license and that you accept its terms.

import logging
import socket
from typing import Dict, Tuple, List, Optional, Set

import lrp


class Address:
    def __init__(self, address):
        if isinstance(address, str):
            self.as_bytes = socket.inet_aton(address)
        elif isinstance(address, bytes):
            if len(address) != 4:
                raise Exception("Unsupported address length for %r" % address)
            self.as_bytes = address
        elif isinstance(address, Address):
            self.as_bytes = address.as_bytes
        else:
            raise TypeError("Unsupported address type: %s" % type(address))

    def __eq__(self, other):
        if isinstance(other, Address):
            return self.as_bytes == other.as_bytes
        if isinstance(other, str):
            return self.__eq__(Address(other))
        return NotImplemented

    def __hash__(self):
        return self.as_bytes.__hash__()

    def __str__(self):
        return socket.inet_ntoa(self.as_bytes)

    def as_subnet(self):
        return "%s/32" % socket.inet_ntoa(self.as_bytes)


# Create special instance
NULL_ADDRESS = Address(b"\x00\x00\x00\x00")
MULTICAST_ADDRESS = Address(lrp.conf['service_multicast_address'])


class Subnet(Address):
    def __init__(self, address, prefix: int = 32):
        if isinstance(address, Address) or isinstance(address, bytes):
            super().__init__(address)
        elif isinstance(address, str):
            parts = address.split("/", 1)
            super().__init__(parts[0])
            if len(parts) > 1:
                # prefix is given in `address`
                mask = parts[1].split(".")
                if len(mask) == 1:
                    # Parse .../32 format
                    self.prefix = int(mask[0])
                else:
                    # Parse .../255.255.255.255 format
                    prefix = format(int.from_bytes(bytes(int(a) for a in mask), 'big'), 'b').find("0")
                    if prefix == -1:
                        prefix = 32
        else:
            raise TypeError("Unexpected type %s" % type(address))
        self.prefix = prefix

    def __contains__(self, item):
        if not isinstance(item, Address):
            raise TypeError("A %s cannot be in a %s" % (type(item).__name__, type(self).__name__))
        if isinstance(item, Subnet) and item.prefix < self.prefix:
            return False
        mask = ((2 ** self.prefix - 1) << (32 - self.prefix))
        return int.from_bytes(self.as_bytes, "big") & mask == \
               int.from_bytes(item.as_bytes, "big") & mask

    def __eq__(self, other):
        if isinstance(other, Subnet):
            return self.as_bytes == other.as_bytes and self.prefix == other.prefix
        if isinstance(other, str):
            return self.__eq__(Subnet(other))
        return NotImplemented

    def __hash__(self):
        if self.prefix == 32:
            # Compatibility with Address
            return super().__hash__()
        else:
            return (self.as_bytes + bytes(self.prefix)).__hash__()

    def __str__(self):
        if self is DEFAULT_ROUTE:
            return "default"
        return "%s/%d" % (socket.inet_ntoa(self.as_bytes), self.prefix)


# Create special instance corresponding to default route
DEFAULT_ROUTE = Subnet(b"\x00\x00\x00\x00", prefix=0)


class RoutingTable:
    logger = logging.getLogger("RoutingTable")

    def __init__(self):
        self.routes: Dict[Subnet, Dict[Address, int]] = {}
        self.neighbors: Set[Address] = set()

    def add_route(self, destination: Subnet, next_hop: Address, metric: int):
        """Add a route to `destination`, through `next_hop`, with cost `metric`. If a
        route with the same destination/next_hop already exists, it is erased
        by the new one. If a route with the same destination but with another
        next_hop exists, they coexists, with their own metric.

        :return True if the route has been inserted in the routing table.
          False if the route was too bad and has not been inserted."""
        try:
            next_hops = self.routes[destination]
        except KeyError:
            # Destination was unknown
            next_hops = self.routes[destination] = {next_hop: metric}
            self.logger.info("Update routing table: new route towards %r through %r[%d]",
                             str(destination), str(next_hop), metric)
        else:
            try:
                known_metric = next_hops[next_hop]
            except KeyError:
                self.logger.info("Update routing table: update route towards %r, also through %r[%d]",
                                 str(destination), str(next_hop), metric)
                next_hops[next_hop] = metric
            else:
                if known_metric <= metric:
                    self.logger.info("Refusing new route: bad metric")
                    return False
                else:
                    self.logger.info("Update routing table: refresh route towards %r through %r[%d]",
                                     str(destination), str(next_hop), metric)
                    next_hops[next_hop] = metric
        return True

    def del_route(self, destination: Subnet, next_hop: Address):
        """Delete the route to `destination`, through `next_hop`. If a route with the
        same destination but with another next_hop exists, the other one
        continues to exist."""
        try:
            next_hops = self.routes[destination]
        except KeyError:
            # Unknown destination, no such next_hop, ok.
            pass
        else:
            try:
                del next_hops[next_hop]
            except KeyError:
                # Was not a next hop, ok.
                pass
            else:
                if len(next_hops) == 0:
                    # No more next hops for this route
                    del self.routes[destination]

    def filter_out_nexthops(self, destination: Subnet, max_metric: int = None) -> List[Tuple[Address, int]]:
        """Filter out some next hops, according to some constraints. Returns the list
        of dropped next hops."""
        try:
            next_hops = self.routes[destination]
        except KeyError:
            # No route, no next hop to filter
            return []
        else:
            dropped = []
            for nh, metric in list(next_hops.items()):
                # Filter according to max_metric
                if max_metric is not None and metric > max_metric:
                    self.logger.debug("Filter next hop %s out of host route towards %s: too big metric (%d)",
                                      nh, destination, max_metric)
                    dropped.append((nh, metric))
                    del next_hops[nh]
            if len(next_hops) == 0:
                # No more next hops for this route
                del self.routes[destination]
            return dropped

    def is_successor(self, neighbor: Address) -> bool:
        """Check if a node is known as a successor."""
        try:
            default_next_hops = self.routes[DEFAULT_ROUTE]
        except KeyError:
            # No default route => no successor at all.
            return False
        else:
            return neighbor in default_next_hops

    def is_predecessor(self, neighbor: Address) -> bool:
        """Check if a node is known as predecessor, i.e. as next-hop for any route"""
        return any(neighbor in next_hops.keys() for next_hops in self.routes.values())

    def get_a_nexthop(self, destination: Address) -> Optional[Address]:
        """Return the best next hop for this destination, according to the metric. If
        many are equal, return any of them."""
        for route_dest, next_hops in sorted(self.routes.items(), key=lambda tple: tple[0].prefix, reverse=True):
            if destination in route_dest:
                best_nh, metric = max(next_hops.items(), key=lambda tple: tple[1])
                return best_nh
        else:
            # No route matches this destination
            return None

    def ensure_is_neighbor(self, neighbor: Address):
        """Check if neighbor is declared. If it is not, add it as neighbor."""
        self.neighbors.add(neighbor)

    def is_neighbor(self, neighbor: Address) -> bool:
        """Check if neighbor is declared. Contrary to `LrpProcess.ensure_is_neighbor`,
        the neighbor is not added if it was not known."""
        return neighbor in self.neighbors

    def __str__(self):
        return "[%s ; %s]" % (
            ", ".join(map(str, self.neighbors)),
            ", ".join("%s: {%s}" % (dest, ", ".join("%s: %d" % (nh, hops) for nh, hops in next_hops.items()))
                      for dest, next_hops in self.routes.items()))
