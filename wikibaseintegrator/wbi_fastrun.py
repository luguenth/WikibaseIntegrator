from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Type, Union

from wikibaseintegrator.datatypes import BaseDataType
from wikibaseintegrator.models import Claim, Claims, Qualifiers, Reference, References
from wikibaseintegrator.wbi_config import config
from wikibaseintegrator.wbi_helpers import execute_sparql_query

log = logging.getLogger(__name__)

fastrun_store: List[FastRunContainer] = []


class FastRunContainer:
    """

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param base_data_type: The default data type to create objects.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive: <not used at this moment>
    :param sparql_endpoint_url: SPARLQ endpoint URL.
    :param wikibase_url: Wikibase URL used for the concept URI.
    """

    # TODO: Add support for case_insensitive

    data: Dict[str, Dict[str, List[Dict[str, str]]]]

    def __init__(self, base_filter: List[BaseDataType | List[BaseDataType]], base_data_type: Optional[Type[BaseDataType]] = None, use_qualifiers: bool = True, use_references: bool = False,
                 cache: bool = True, case_insensitive: bool = False, sparql_endpoint_url: Optional[str] = None, wikibase_url: Optional[str] = None):

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
        self.cache = cache
        self.case_insensitive = case_insensitive
        self.properties_type: Dict[str, str] = {}

        if self.case_insensitive:
            raise ValueError("Case insensitive does not work for the moment.")

    def load_statements(self, claims: Union[List[Claim], Claims, Claim], cache: Optional[bool] = None, wb_url: Optional[str] = None, limit: Optional[int] = None) -> None:
        """
        Load the statements related to the given claims into the internal cache of the current object.

        :param claims: A Claim, Claims or list of Claim
        :param wb_url: The first part of the concept URI of entities.
        :param limit: The limit to request at one time.
        :param cache: Put data returned by WDQS in cache. Enabled by default.
        :return:
        """
        if isinstance(claims, Claim):
            claims = [claims]
        elif (not isinstance(claims, list) or not all(isinstance(n, Claim) for n in claims)) and not isinstance(claims, Claims):
            raise ValueError("claims must be an instance of Claim or Claims or a list of Claim")

        cache = bool(cache or self.cache)

        wb_url = wb_url or self.wikibase_url

        limit = limit or int(config['SPARQL_QUERY_LIMIT'])  # type: ignore

        for claim in claims:
            prop_nr = claim.mainsnak.property_number

            # Load each property from the Wikibase instance or the cache
            if cache and prop_nr in self.data:
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
                # if claim.mainsnak.datavalue:
                #     query = '''
                #     #Tool: WikibaseIntegrator wbi_fastrun.load_statements
                #     SELECT ?entity ?sid ?value ?property_type WHERE {{
                #       # Base filter string
                #       {base_filter_string}
                #       ?entity <{wb_url}/prop/{prop_nr}> ?sid.
                #       <{wb_url}/entity/{prop_nr}> wikibase:propertyType ?property_type.
                #       ?sid <{wb_url}/prop/statement/{prop_nr}> ?value.
                #       ?sid <{wb_url}/prop/statement/{prop_nr}> {value}.
                #       {qualifiers_filter_string}
                #     }}
                #     ORDER BY ?sid
                #     OFFSET {offset}
                #     LIMIT {limit}
                #     '''
                #
                #     # Format the query
                #     query = query.format(base_filter_string=base_filter_string, wb_url=wb_url, prop_nr=prop_nr, offset=str(offset), limit=str(limit),
                #                          value=claim.get_sparql_value(wikibase_url=wb_url), qualifiers_filter_string=qualifiers_filter_string)
                # else:
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

    def _load_qualifiers(self, sid: str, limit: Optional[int] = None) -> Qualifiers:
        """
        Load the qualifiers of a statement.

        :param sid: A statement ID.
        :param limit: The limit to request at one time.
        :return: A Qualifiers object.
        """
        offset = 0

        limit = limit or int(config['SPARQL_QUERY_LIMIT'])  # type: ignore

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

    def get_entities(self, claims: Union[List[Claim], Claims, Claim], cache: Optional[bool] = None, query_limit: Optional[int] = None) -> List[str]:
        """
        Return a list of entities who correspond to the specified claims.

        :param claims: A list of claims to query the SPARQL endpoint.
        :param cache: Put data returned by WDQS in cache. Enabled by default.
        :param query_limit: Limit the amount of results from the SPARQL server
        :return: A list of entity ID.
        """
        if isinstance(claims, Claim):
            claims = [claims]
        elif (not isinstance(claims, list) or not all(isinstance(n, Claim) for n in claims)) and not isinstance(claims, Claims):
            raise ValueError("claims must be an instance of Claim or Claims or a list of Claim")

        self.load_statements(claims=claims, cache=cache, limit=query_limit)

        result = []
        for base_filter in self.base_filter:
            sub_result = set()
            if isinstance(base_filter, BaseDataType):  # TODO: Manage case where filter is a list of BaseDataType
                if not base_filter.mainsnak.datavalue:
                    for claim in claims:
                        if base_filter.mainsnak.property_number == claim.mainsnak.property_number:
                            # Add the returned entities to the result list
                            if claim.get_sparql_value() in self.data[claim.mainsnak.property_number]:
                                for rez in self.data[claim.mainsnak.property_number][claim.get_sparql_value()]:  # type: ignore
                                    sub_result.add(rez['entity'].rsplit('/', 1)[-1])
                else:
                    if base_filter.mainsnak.property_number in self.data:
                        if base_filter.get_sparql_value() in self.data[base_filter.mainsnak.property_number]:
                            for rez in self.data[base_filter.mainsnak.property_number][base_filter.get_sparql_value()]:  # type: ignore
                                sub_result.add(rez['entity'].rsplit('/', 1)[-1])
                    else:
                        continue
            result.append(sub_result)

        if result:
            if len(result) > 1:
                return list(set(result[0]).intersection(*result[1:]))
            return list(result[0])
        else:
            return []

    def write_required(self, claims: Union[List[Claim], Claims, Claim], entity_filter: Union[List[str], str, None] = None, property_filter: Union[List[str], str, None] = None, use_qualifiers: Optional[bool] = None,
                       use_references: Optional[bool] = None, cache: Optional[bool] = None, query_limit: Optional[int] = None) -> bool:
        """

        :param claims:
        :param entity_filter: Allows you to filter the entities checked. This can be a single entity or a list of entities.
        :param property_filter: Allows you to limit the difference comparison to a list of properties
        :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
        :param use_references: Use references during fastrun. Disabled by default.
        :param cache: Put data returned by WDQS in cache. Enabled by default.
        :param query_limit: Limit the amount of results from the SPARQL server
        :return: a boolean True if a write is required. False otherwise.
        """

        if isinstance(claims, Claim):
            claims = [claims]
        elif (not isinstance(claims, list) or not all(isinstance(n, Claim) for n in claims)) and not isinstance(claims, Claims):
            raise ValueError("claims must be an instance of Claim or Claims or a list of Claim")

        if len(claims) == 0:
            raise ValueError("claims must have at least one claim")

        if entity_filter is not None and isinstance(entity_filter, str):
            entity_filter = [entity_filter]

        if property_filter is not None and isinstance(property_filter, str):
            property_filter = [property_filter]

        # Generate a property_filter if None is given
        if property_filter is None:
            property_filter = [claim.mainsnak.property_number for claim in claims]

        use_qualifiers = bool(use_qualifiers or self.use_qualifiers)
        use_references = bool(use_references or self.use_references)

        def contains(in_list, lambda_filter):
            for x in in_list:
                if lambda_filter(x):
                    return True
            return False

        # Get all the potential statements
        statements_to_check: Dict[str, List[str]] = {}
        for claim in claims:
            if claim.mainsnak.property_number in property_filter:
                self.load_statements(claims=claim, cache=cache, limit=query_limit)
                if claim.mainsnak.property_number in self.data:
                    if not contains(self.data[claim.mainsnak.property_number], (lambda x, c=claim: x == c.get_sparql_value())):
                        # Checks if a property with this value does not exist, return True if none exist
                        logging.debug("Value '%s' does not exist for property '%s'", claim.get_sparql_value(), claim.mainsnak.property_number)
                        return True
                        # TODO: Doesn't work in the value already exists in another entity

                    sparql_value = claim.get_sparql_value()
                    if sparql_value:
                        for statement in self.data[claim.mainsnak.property_number][sparql_value]:
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

        # If there is none common entities, return True because we need a write
        if not common_entities:
            logging.debug("There is no common entities")
            return True

        # If the property is already found, load it completely to compare deeply
        for claim in claims:
            if claim.mainsnak.property_number in property_filter:
                sparql_value = claim.get_sparql_value()
                if sparql_value and claim.mainsnak.property_number in self.data and sparql_value in self.data[claim.mainsnak.property_number]:
                    for statement in self.data[claim.mainsnak.property_number][sparql_value]:
                        if entity_filter and statement['entity'].rsplit('/', 1)[-1] not in entity_filter:
                            continue
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

                                if sum(len(ref) for ref in references) != sum(len(x) for x in claim.references):
                                    logging.debug("Difference in number of references, '%i' != '%i'", sum(len(ref) for ref in references), sum(len(x) for x in claim.references))
                                    return True

                                for reference in references:
                                    if reference not in claim.references:
                                        logging.debug("Difference between two references")
                                        return True

                            # TODO: Add use_rank to compare rank ?

        return False


def get_fastrun_container(base_filter: List[BaseDataType | List[BaseDataType]], use_qualifiers: bool = True, use_references: bool = False, cache: bool = True,
                          case_insensitive: bool = False) -> FastRunContainer:
    """
    Return a FastRunContainer object, create a new one if it doesn't already exist.

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive:
    :return: a FastRunContainer object
    """
    if base_filter is None:
        base_filter = []

    # We search if we already have a FastRunContainer with the same parameters to re-use it
    fastrun_container = _search_fastrun_store(base_filter=base_filter, use_qualifiers=use_qualifiers, use_references=use_references, case_insensitive=case_insensitive,
                                              cache=cache)

    return fastrun_container


def _search_fastrun_store(base_filter: List[BaseDataType | List[BaseDataType]], use_qualifiers: bool = True, use_references: bool = False, cache: bool = True,
                          case_insensitive: bool = False) -> FastRunContainer:
    """
    Search for an existing FastRunContainer with the same parameters or create a new one if it doesn't exist.

    :param base_filter: The default filter to initialize the dataset. A list made of BaseDataType or list of BaseDataType.
    :param use_qualifiers: Use qualifiers during fastrun. Enabled by default.
    :param use_references: Use references during fastrun. Disabled by default.
    :param cache: Put data returned by WDQS in cache. Enabled by default.
    :param case_insensitive:
    :return: a FastRunContainer object
    """
    for fastrun in fastrun_store:
        if (fastrun.base_filter == base_filter) and (fastrun.use_qualifiers == use_qualifiers) and (fastrun.use_references == use_references) and (
                fastrun.case_insensitive == case_insensitive) and (fastrun.sparql_endpoint_url == config['SPARQL_ENDPOINT_URL']):
            fastrun.cache = cache
            return fastrun

    # In case nothing was found in the fastrun_store
    log.info("Create a new FastRunContainer")

    fastrun_container = FastRunContainer(base_data_type=BaseDataType, base_filter=base_filter, use_qualifiers=use_qualifiers, use_references=use_references, cache=cache,
                                         case_insensitive=case_insensitive)
    fastrun_store.append(fastrun_container)
    return fastrun_container
