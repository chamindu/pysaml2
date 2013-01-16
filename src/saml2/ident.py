import copy
import shelve
from hashlib import sha256
from urllib import quote
from urllib import unquote
from saml2.s_utils import rndstr
from saml2.s_utils import PolicyError
from saml2.saml import NameID
from saml2.saml import NAMEID_FORMAT_TRANSIENT
from saml2.saml import NAMEID_FORMAT_EMAILADDRESS

__author__ = 'rolandh'

ATTR = ["name_qualifier", "sp_name_qualifier", "format", "sp_provided_id"]

class Unknown(Exception):
    pass

def code(item):
    _res = []
    i = 0
    for attr in ATTR:
        val = getattr(item, attr)
        if val:
            _res.append("%d=%s" % (i, quote(val)))
        i += 1
    return ",".join(_res)

def decode(str):
    _nid = NameID()
    for part in str.split(","):
        i, val = part.split("=")
        setattr(_nid, ATTR[int(i)], unquote(val))
    return _nid

class IdentDB(object):
    """ A class that handles identifiers of entities
     Keeps a list of all nameIDs returned per SP
    """
    def __init__(self, db, domain="", name_qualifier=""):
        if isinstance(db, basestring):
            self.db = shelve.open(db)
        else:
            self.db = db
        self.domain = domain
        self.name_qualifier = name_qualifier

    def _create_id(self, format, name_qualifier="", sp_name_qualifier=""):
        _id = sha256(rndstr(32))
        _id.update(format)
        if name_qualifier:
            _id.update(name_qualifier)
        if sp_name_qualifier:
            _id.update(sp_name_qualifier)
        return _id.hexdigest()

    def create_id(self, format, name_qualifier="", sp_name_qualifier=""):
        _id = self._create_id(format, name_qualifier, sp_name_qualifier)
        while _id in self.db:
            _id = self._create_id(format, name_qualifier, sp_name_qualifier)
        return _id

    def store(self, id, name_id):
        try:
            val = self.db[id].split(" ")
        except KeyError:
            val = []

        _cn = code(name_id)
        val.append(_cn)
        self.db[id] = " ".join(val)
        self.db[_cn] = id

    def remove_remote(self, name_id):
        _cn = code(name_id)
        _id = self.db[_cn]
        try:
            vals = self.db[_id].split(" ")
            vals.remove(_cn)
            self.db[id] = " ".join(vals)
        except KeyError:
            pass

        del self.db[_cn]

    def remove_local(self, id):
        try:
            for val in self.db[id].split(" "):
                try:
                    del self.db[val]
                except KeyError:
                    pass
            del self.db[id]
        except KeyError:
            pass

    def get_nameid(self, format, sp_name_qualifier, userid, name_qualifier):
        _id = self.create_id(format, name_qualifier, sp_name_qualifier)

        if format == NAMEID_FORMAT_EMAILADDRESS:
            if not self.domain:
                raise Exception("Can't issue email nameids, unknown domain")

            _id = "%s@%s" % (_id, self.domain)

        nameid = NameID(format=format, sp_name_qualifier=sp_name_qualifier,
                        name_qualifier=name_qualifier, text=_id)

        self.store(userid, nameid)
        return nameid

    def nim_args(self, local_policy=None, sp_name_qualifier="",
                 name_id_policy=None, name_qualifier=""):
        """

        :param local_policy:
        :param sp_name_qualifier:
        :param name_id_policy:
        :param name_qualifier:
        :return:
        """
        if name_id_policy and name_id_policy.sp_name_qualifier:
            sp_name_qualifier = name_id_policy.sp_name_qualifier
        else:
            sp_name_qualifier = sp_name_qualifier

        if name_id_policy:
            nameid_format = name_id_policy.format
        elif local_policy:
            nameid_format = local_policy.get_nameid_format(sp_name_qualifier)
        else:
            raise Exception("Unknown NameID format")

        if not name_qualifier:
            name_qualifier = self.name_qualifier

        return {"format":nameid_format, "sp_name_qualifier": sp_name_qualifier,
                "name_qualifier":name_qualifier}

    def construct_nameid(self, userid, local_policy=None,
                         sp_name_qualifier=None, name_id_policy=None,
                         sp_nid=None, name_qualifier=""):
        """ Returns a name_id for the object. How the name_id is
        constructed depends on the context.

        :param local_policy: The policy the server is configured to follow
        :param userid: The local permanent identifier of the object
        :param sp_name_qualifier: The 'user'/-s of the name_id
        :param name_id_policy: The policy the server on the other side wants
            us to follow.
        :param sp_nid: Name ID Formats from the SPs metadata
        :return: NameID instance precursor
        """

        args = self.nim_args(local_policy, sp_name_qualifier, name_id_policy)
        return self.get_nameid(userid, **args)

    def find_local_id(self, name_id):
        """
        Only find on persistent IDs

        :param name_id:
        :return:
        """

        try:
            return self.db[code(name_id)]
        except KeyError:
            return None

    def match_local_id(self, userid, sp_name_qualifier, name_qualifier):
        try:
            for val in self.db[userid].split(" "):
                nid = decode(val)
                if nid.format == NAMEID_FORMAT_TRANSIENT:
                    continue
                if getattr(nid, "sp_name_qualifier", "") == sp_name_qualifier:
                    if getattr(nid, "name_qualifier", "") == name_qualifier:
                        return nid
        except KeyError:
            pass

        return None

    def handle_name_id_mapping_request(self, name_id, name_id_policy):
        """

        :param name_id: The NameID that specifies the principal
        :param name_id_policy: The NameIDPolicy of the requester
        :return: If an old name_id exists that match the name-id policy
            that is return otherwise if a new one can be created it
            will be and returned. If no old matching exists and a new
            is not allowed to be created None is returned.
        """
        _id = self.find_local_id(name_id)
        if not _id:
            raise Unknown("Unknown entity")

        # return an old one if present
        for val in self.db[_id].split(" "):
            _nid = decode(val)
            if _nid.format == name_id_policy.format:
                if _nid.sp_name_qualifier == name_id_policy.sp_name_qualifier:
                    return _nid

        if name_id_policy.allow_create == "false":
            raise PolicyError("Not allowed to create new identifier")

        # else create and return a new one
        return self.construct_nameid(_id, name_id_policy=name_id_policy)

    def handle_manage_name_id_request(self, name_id, new_id="",
                                      new_encrypted_id="", terminate=""):
        """
        Requests from the SP is about the SPProvidedID attribute.
        So this is about adding,replacing and removing said attribute.

        :param name_id:
        :param new_id:
        :param new_encrypted_id:
        :param terminate:
        :return:
        """
        _id = self.find_local_id(name_id)

        orig_name_id = copy.copy(name_id)

        if new_id:
            name_id.sp_provided_id = new_id
        elif new_encrypted_id:
            # TODO
            pass
        elif terminate:
            name_id.sp_provided_id = None
        else:
            #NOOP
            return True

        self.remove_remote(orig_name_id)
        self.store(id, name_id)
        return True

    def publish(self, userid, name_id, entity_id):
        """
        About userid I have published nameid to entity_id
        Will gladly overwrite whatever was there before
        :param userid:
        :param name_id:
        :param entity_id:
        :return:
        """

        self.db["%s:%s" % (userid, entity_id)] = name_id

    def published(self, userid, entity_id):
        """

        :param userid:
        :param entity_id:
        :return:
        """
        try:
            return self.db["%s:%s" % (userid, entity_id)]
        except KeyError:
            return None