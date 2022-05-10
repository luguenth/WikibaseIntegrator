from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Dict, List, Optional, Type, Union

from wikibaseintegrator.datatypes import BaseDataType
from wikibaseintegrator.models import Claim, Claims, Qualifiers, Reference, References
from wikibaseintegrator.wbi_config import config
from wikibaseintegrator.wbi_helpers import execute_sparql_query

if TYPE_CHECKING:
    from wikibaseintegrator.entities import BaseEntity

log = logging.getLogger(__name__)

fastrun_store: List[FastRunContainer] = []


class FastRunContainer:
    """

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param base_data_type: The default data type to create objects.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param use_cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive: <not used at this moment>
    :param sparql_endpoint_url: SPARLQ endpoint URL.
    :param wikibase_url: Wikibase URL used for the concept URI.
    """

    # TODO: Add support for case_insensitive

    data: Dict[str, Dict[str, List[Dict[str, str]]]]

    def __init__(self, base_filter: List[BaseDataType | List[BaseDataType]], base_data_type: Optional[Type[BaseDataType]] = None, use_qualifiers: bool = True, use_references: bool = False,
                 use_cache: bool = True, case_insensitive: bool = False, sparql_endpoint_url: Optional[str] = None, wikibase_url: Optional[str] = None):

        for k in base_filter:
            if not isinstance(k, BaseDataType) and not (isinstance(k, list) and len(k) == 2 and isinstance(k[0], BaseDataType) and isinstance(k[1], BaseDataType)):
                raise ValueError("base_filter must be an instance of BaseDataType or a list of instances of BaseDataType")

        self.data: Dict[str, Dict[str, List[Dict[str, str]]]] = {}

        self.base_filter = base_filter
        self.base_data_type = base_data_type or BaseDataType
        self.sparql_endpoint_url = str(sparql_endpoint_url or config['SPARQL_ENDPOINT_URL'])
        self.wikibase_url = str(wikibase_url or config['WIKIBASE_URL'])
        self.use_qualifiers = use_qualifiers
        self.use_references = use_references
        self.use_cache = use_cache
        self.case_insensitive = case_insensitive
        self.properties_type: Dict[str, str] = {}

        if self.case_insensitive:
            raise ValueError("Case insensitive does not work for the moment.")

    def load_statements(self, claims: Union[List[Claim], Claims, Claim], use_cache: Optional[bool] = None, wb_url: Optional[str] = None, limit: int = 10000) -> None:
        """
        Load the statements related to the given claims into the internal cache of the current object.

        :param claims: A Claim, Claims or list of Claim
        :param wb_url: The first part of the concept URI of entities.
        :param limit: The limit to request at one time.
        :param use_cache: Put data returned by WDQS in cache. Enabled by default.
        :return:
        """
        if isinstance(claims, Claim):
            claims = [claims]
        elif (not isinstance(claims, list) or not all(isinstance(n, Claim) for n in claims)) and not isinstance(claims, Claims):
            raise ValueError("claims must be an instance of Claim or Claims or a list of Claim")

        use_cache = bool(use_cache or self.use_cache)

        wb_url = wb_url or self.wikibase_url

        for claim in claims:
            prop_nr = claim.mainsnak.property_number

            # Load each property from the Wikibase instance or the cache
            if use_cache and prop_nr in self.data:
                continue

            offset = 0

            # Generate base filter
            base_filter_string = ''
            for k in self.base_filter:
                if isinstance(k, BaseDataType):
                    if k.mainsnak.datavalue:
                        base_filter_string += '?entity <{wb_url}/prop/direct/{prop_nr}> {entity} .\n'.format(
                            wb_url=wb_url, prop_nr=k.mainsnak.property_number, entity=k.get_sparql_value(wikibase_url=wb_url))
                    elif sum(map(lambda x, other=k: x.mainsnak.property_number == other.mainsnak.property_number, self.base_filter)) == 1:  # type: ignore
                        base_filter_string += '?entity <{wb_url}/prop/direct/{prop_nr}> ?zz{prop_nr} .\n'.format(
                            wb_url=wb_url, prop_nr=k.mainsnak.property_number)
                elif isinstance(k, list) and len(k) == 2 and isinstance(k[0], BaseDataType) and isinstance(k[1], BaseDataType):
                    if k[0].mainsnak.datavalue:
                        base_filter_string += '?entity <{wb_url}/prop/direct/{prop_nr}>/<{wb_url}/prop/direct/{prop_nr2}>* {entity} .\n'.format(
                            wb_url=wb_url, prop_nr=k[0].mainsnak.property_number, prop_nr2=k[1].mainsnak.property_number,
                            entity=k[0].get_sparql_value(wikibase_url=wb_url))
                    # TODO: Remove ?zzPYY if another filter have the same property number, the same as above
                    else:
                        base_filter_string += '?entity <{wb_url}/prop/direct/{prop_nr1}>/<{wb_url}/prop/direct/{prop_nr2}>* ?zz{prop_nr1}{prop_nr2} .\n'.format(
                            wb_url=wb_url, prop_nr1=k[0].mainsnak.property_number, prop_nr2=k[1].mainsnak.property_number)
                else:
                    raise ValueError("base_filter must be an instance of BaseDataType or a list of instances of BaseDataType")

            qualifiers_filter_string = ''
            if self.use_qualifiers:
                for qualifier in claim.qualifiers:
                    fake_json = {
                        'mainsnak': qualifier.get_json(),
                        'type': qualifier.datatype,
                        'id': 'Q0',
                        'rank': 'normal'
                    }
                    f = [x for x in self.base_data_type.subclasses if x.DTYPE == qualifier.datatype][0]().from_json(json_data=fake_json)
                    qualifiers_filter_string += f'?sid pq:{qualifier.property_number} {f.get_sparql_value()}.\n'

            # We force a refresh of the data, remove the previous results
            self.data[prop_nr] = {}

            while True:
                query = '''
                #Tool: WikibaseIntegrator wbi_fastrun.load_statements
                SELECT ?entity ?sid ?value ?property_type WHERE {{
                  # Base filter string
                  {base_filter_string}
                  ?entity <{wb_url}/prop/{prop_nr}> ?sid.
                  <{wb_url}/entity/{prop_nr}> wikibase:propertyType ?property_type.
                  ?sid <{wb_url}/prop/statement/{prop_nr}> ?value.
                  {qualifiers_filter_string}
                }}
                ORDER BY ?sid
                OFFSET {offset}
                LIMIT {limit}
                '''

                # Format the query
                query = query.format(base_filter_string=base_filter_string, wb_url=wb_url, prop_nr=prop_nr, offset=str(offset), limit=str(limit),
                                     qualifiers_filter_string=qualifiers_filter_string)
                offset += limit  # We increase the offset for the next iteration
                results = execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings']

                for result in results:
                    entity = result['entity']['value']
                    sid = result['sid']['value']
                    # value = result['value']['value']
                    property_type = result['property_type']['value']

                    # Use casefold for lower case
                    if self.case_insensitive:
                        result['value']['value'] = result['value']['value'].casefold()

                    f = [x for x in self.base_data_type.subclasses if x.PTYPE == property_type][0]().from_sparql_value(sparql_value=result['value'])

                    sparql_value = f.get_sparql_value()
                    if sparql_value is not None:
                        if sparql_value not in self.data[prop_nr]:
                            self.data[prop_nr][sparql_value] = []

                        if prop_nr not in self.properties_type:
                            self.properties_type[prop_nr] = property_type

                        self.data[prop_nr][sparql_value].append({'entity': entity, 'sid': sid})

                if len(results) == 0 or len(results) < limit:
                    break

    def _load_qualifiers(self, sid: str, limit: int = 10000) -> Qualifiers:
        """
        Load the qualifiers of a statement.

        :param sid: A statement ID.
        :param limit: The limit to request at one time.
        :return: A Qualifiers object.
        """
        offset = 0

        # We force a refresh of the data, remove the previous results
        qualifiers: Qualifiers = Qualifiers()
        while True:
            query = f'''
            #Tool: WikibaseIntegrator wbi_fastrun._load_qualifiers
            SELECT ?property ?value ?property_type WHERE {{
              VALUES ?sid {{ <{sid}> }}
              ?sid ?predicate ?value.
              ?property wikibase:qualifier ?predicate.
              ?property wikibase:propertyType ?property_type.
            }}
            ORDER BY ?sid
            OFFSET {offset}
            LIMIT {limit}
            '''

            # Format the query
            # query = query.format(wb_url=wb_url, sid=sid, offset=str(offset), limit=str(limit))
            offset += limit  # We increase the offset for the next iteration
            results = execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings']

            for result in results:
                property = result['property']['value']
                property_type = result['property_type']['value']

                if property not in self.properties_type:
                    self.properties_type[property] = property_type

                # Use casefold for lower case
                if self.case_insensitive:
                    result['value']['value'] = result['value']['value'].casefold()

                f = [x for x in self.base_data_type.subclasses if x.PTYPE == property_type][0](prop_nr=property).from_sparql_value(sparql_value=result['value'])
                qualifiers.add(f)

            if len(results) == 0 or len(results) < limit:
                break

        return qualifiers

    def _load_references(self, sid: str, limit: int = 10000) -> References:
        """
        Load the references of a statement.

        :param sid: A statement ID.
        :param limit: The limit to request at one time.
        :return: A References object.
        """
        offset = 0

        if not isinstance(sid, str):
            raise ValueError('sid must be a string')

        # We force a refresh of the data, remove the previous results
        references: References = References()
        while True:
            query = f'''
            #Tool: WikibaseIntegrator wbi_fastrun._load_references
            SELECT ?srid ?ref_property ?ref_value ?property_type WHERE {{
              VALUES ?sid {{ <{sid}> }}

              ?sid prov:wasDerivedFrom ?srid.
              ?srid ?ref_predicate ?ref_value.
              ?ref_property wikibase:reference ?ref_predicate.
              ?ref_property wikibase:propertyType ?property_type.
            }}
            ORDER BY ?srid
            OFFSET {offset}
            LIMIT {limit}
            '''

            # Format the query
            # query = query.format(wb_url=wb_url, sid=sid, offset=str(offset), limit=str(limit))
            offset += limit  # We increase the offset for the next iteration
            results = execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings']

            reference = {}

            for result in results:
                ref_property = result['ref_property']['value']
                srid = result['srid']['value']
                property_type = result['property_type']['value']

                if ref_property not in self.properties_type:
                    self.properties_type[ref_property] = property_type

                # Use casefold for lower case
                if self.case_insensitive:
                    result['value']['value'] = result['value']['value'].casefold()

                f = [x for x in self.base_data_type.subclasses if x.PTYPE == property_type][0](prop_nr=ref_property).from_sparql_value(sparql_value=result['ref_value'])

                if srid not in reference:
                    reference[srid] = Reference()

                reference[srid].add(f)

            # Add each Reference to the References
            for _, ref in reference.items():
                references.add(ref)

            if len(results) == 0 or len(results) < limit:
                break

        return references

    def _get_property_type(self, prop_nr: Union[str, int]) -> str:
        """
        Obtain the property type of the given property by looking at the SPARQL endpoint.

        :param prop_nr: The property number.
        :return: The SPARQL version of the property type.
        """
        if isinstance(prop_nr, int):
            prop_nr = 'P' + str(prop_nr)
        elif prop_nr is not None:
            pattern = re.compile(r'^P?([0-9]+)$')
            matches = pattern.match(prop_nr)

            if not matches:
                raise ValueError('Invalid prop_nr, format must be "P[0-9]+"')

            prop_nr = 'P' + str(matches.group(1))

        query = f'''#Tool: WikibaseIntegrator wbi_fastrun._get_property_type
        SELECT ?property_type WHERE {{ wd:{prop_nr} wikibase:propertyType ?property_type. }}'''

        results = execute_sparql_query(query=query, endpoint=self.sparql_endpoint_url)['results']['bindings'][0]['property_type']['value']

        return results

    def get_entities(self, claims: Union[List[Claim], Claims, Claim], use_cache: Optional[bool] = None) -> List[str]:
        """
        Return a list of entities who correspond to the specified claims.

        :param claims: A list of claims to query the SPARQL endpoint.
        :param use_cache: Put data returned by WDQS in cache. Enabled by default.
        :return: A list of entity ID.
        """
        if isinstance(claims, Claim):
            claims = [claims]
        elif (not isinstance(claims, list) or not all(isinstance(n, Claim) for n in claims)) and not isinstance(claims, Claims):
            raise ValueError("claims must be an instance of Claim or Claims or a list of Claim")

        self.load_statements(claims=claims, use_cache=use_cache)

        result = set()
        for claim in claims:
            # Add the returned entities to the result list
            for dat in self.data[claim.mainsnak.property_number]:
                for rez in self.data[claim.mainsnak.property_number][dat]:
                    result.add(rez['entity'].rsplit('/', 1)[-1])

        return list(result)

    def write_required(self, entity: BaseEntity, property_filter: Union[List[str], str, None] = None, use_qualifiers: Optional[bool] = None, use_references: Optional[bool] = None,
                       use_cache: Optional[bool] = None) -> bool:
        """

        :param entity:
        :param property_filter:
        :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
        :param use_references: Use references during fastrun. Disabled by default.
        :param use_cache: Put data returned by WDQS in cache. Enabled by default.
        :return: a boolean True if a write is required. False otherwise.
        """
        from wikibaseintegrator.entities import BaseEntity

        if not isinstance(entity, BaseEntity):
            raise ValueError("entity must be an instance of BaseEntity")

        if len(entity.claims) == 0:
            raise ValueError("entity must have at least one claim")

        if property_filter is not None and isinstance(property_filter, str):
            property_filter = [property_filter]

        # Generate a property_filter if None is given
        if property_filter is None:
            property_filter = [claim.mainsnak.property_number for claim in entity.claims]

        use_qualifiers = bool(use_qualifiers or self.use_qualifiers)
        use_references = bool(use_references or self.use_references)

        def contains(in_list, lambda_filter):
            for x in in_list:
                if lambda_filter(x):
                    return True
            return False

        # Get all the potential statements
        statements_to_check: Dict[str, List[str]] = {}
        for claim in entity.claims:
            if claim.mainsnak.property_number in property_filter:
                self.load_statements(claims=claim, use_cache=use_cache)
                if claim.mainsnak.property_number in self.data:
                    if not contains(self.data[claim.mainsnak.property_number], (lambda x, c=claim: x == c.get_sparql_value())):
                        # Found if a property with this value does not exist, return True if none exist
                        logging.debug("Value '%s' does not exist for property '%s'", claim.get_sparql_value(), claim.mainsnak.property_number)
                        return True

                    for statement in self.data[claim.mainsnak.property_number][claim.get_sparql_value()]:
                        if claim.mainsnak.property_number not in statements_to_check:
                            statements_to_check[claim.mainsnak.property_number] = []
                        statements_to_check[claim.mainsnak.property_number].append(statement['entity'])

        # Generate an intersection between all the statements by property, based on the entity
        # Generate only the list of entities
        list_entities: List[List[str]] = []
        for _, statements in statements_to_check.items():
            # entities = [statement['entity'] for statement in statements_to_check[property]]
            list_entities.append(list(set(statements)))

        # Return the intersection between all the list
        common_entities: List = list_entities.pop()
        for entities in list_entities:
            common_entities = list(set(common_entities).intersection(entities))

        # If the property is already found, load it completely to compare deeply
        for claim in entity.claims:
            if claim.mainsnak.property_number in property_filter:
                sparql_value: str = claim.get_sparql_value()
                if claim.mainsnak.property_number in self.data and sparql_value in self.data[claim.mainsnak.property_number]:
                    for statement in self.data[claim.mainsnak.property_number][sparql_value]:
                        if statement['entity'] in common_entities:
                            if use_qualifiers:
                                qualifiers = self._load_qualifiers(statement['sid'], limit=100)

                                if len(qualifiers) != len(claim.qualifiers):
                                    logging.debug("Difference in number of qualifiers, '%i' != '%i'", len(qualifiers), len(claim.qualifiers))
                                    return True

                                for qualifier in qualifiers:
                                    if qualifier not in claim.qualifiers:
                                        logging.debug("Difference between two qualifiers")
                                        return True

                            if use_references:
                                references = self._load_references(statement['sid'], limit=100)

                                if len(references) != len(claim.references):
                                    logging.debug("Difference in number of references, '%i' != '%i'", len(references), len(claim.references))
                                    return True

                                for reference in references:
                                    if reference not in claim.references:
                                        logging.debug("Difference between two references")
                                        return True

        return False


def get_fastrun_container(base_filter: List[BaseDataType | List[BaseDataType]], use_qualifiers: bool = True, use_references: bool = False, use_cache: bool = True,
                          case_insensitive: bool = False) -> FastRunContainer:
    """
    Return a FastRunContainer object, create a new one if it doesn't already exist.

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param use_cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive:
    :return: a FastRunContainer object
    """
    if base_filter is None:
        base_filter = []

    # We search if we already have a FastRunContainer with the same parameters to re-use it
    fastrun_container = _search_fastrun_store(base_filter=base_filter, use_qualifiers=use_qualifiers, use_references=use_references, case_insensitive=case_insensitive,
                                              use_cache=use_cache)

    return fastrun_container


def _search_fastrun_store(base_filter: List[BaseDataType | List[BaseDataType]], use_qualifiers: bool = True, use_references: bool = False, use_cache: bool = True,
                          case_insensitive: bool = False) -> FastRunContainer:
    """
    Search for an existing FastRunContainer with the same parameters or create a new one if it doesn't exist.

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param use_cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive:
    :return: a FastRunContainer object
    """
    for fastrun in fastrun_store:
        if (fastrun.base_filter == base_filter) and (fastrun.use_qualifiers == use_qualifiers) and (fastrun.use_references == use_references) and (
                fastrun.case_insensitive == case_insensitive) and (fastrun.sparql_endpoint_url == config['SPARQL_ENDPOINT_URL']):
            fastrun.use_cache = use_cache
            return fastrun

    # In case nothing was found in the fastrun_store
    log.info("Create a new FastRunContainer")

    fastrun_container = FastRunContainer(base_data_type=BaseDataType, base_filter=base_filter, use_qualifiers=use_qualifiers, use_references=use_references, use_cache=use_cache,
                                         case_insensitive=case_insensitive)
    fastrun_store.append(fastrun_container)
    return fastrun_container
