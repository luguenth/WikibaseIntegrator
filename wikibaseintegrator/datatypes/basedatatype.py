from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Type, Union

from wikibaseintegrator.models import Claim


class BaseDataType(Claim):
    """
    The base class for all Wikibase data types, they inherit from it
    """
    DTYPE = 'base-data-type'
    PTYPE = 'property-data-type'
    subclasses: List[Type[BaseDataType]] = []
    sparql_query: str = '''
        SELECT * WHERE {{
          ?item_id <{wb_url}/prop/{pid}> ?s .
          ?s <{wb_url}/prop/statement/{pid}> '{value}' .
        }}
    '''

    def __init__(self, prop_nr: Optional[Union[int, str]] = None, **kwargs: Any):
        """
        Constructor, will be called by all data types.

        :param prop_nr: The property number a Wikibase snak belongs to
        """

        super().__init__(**kwargs)

        if isinstance(prop_nr, str):
            pattern = re.compile(r'^([a-z][a-z\d+.-]*):([^][<>\"\x00-\x20\x7F])+$')
            matches = pattern.match(str(prop_nr))

            if matches:
                prop_nr = prop_nr.rsplit('/', 1)[-1]

        self.mainsnak.property_number = prop_nr
        # self.subclasses.append(self)

    # Allow registration of subclasses of BaseDataType into BaseDataType.subclasses
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__(**kwargs)
        cls.subclasses.append(cls)

    def set_value(self, value: Optional[Any] = None):
        pass

    def get_sparql_value(self, **kwargs: Any) -> Optional[str]:
        return '"' + self.mainsnak.datavalue['value'] + '"'

    def parse_sparql_value(self, value, type='literal', unit='1') -> bool:
        if type == 'uri':
            pattern = re.compile(r'^<?(.*?)>?$')
            matches = pattern.match(value)
            if not matches:
                return False

            self.set_value(value=matches.group(1))
        elif type == 'literal':
            pattern = re.compile(r'^"?(.*?)"?$')
            matches = pattern.match(value)
            if not matches:
                return False

            self.set_value(value=matches.group(1))
        else:
            raise ValueError

        return True

    def from_sparql_value(self, sparql_value: Dict) -> BaseDataType:  # type: ignore
        pass
