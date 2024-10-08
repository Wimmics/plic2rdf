"""
This module provides funtions to parse an XML schema definition (XSD) and 
progressively generate an OWL ontology from it.

Author: Franck Michel, Université Côte d'Azur, CNRS, Inria
Created: Aug. 2024
"""

import logging
from pprint import pformat
from rdflib.namespace import XSD, URIRef
from traceback import format_exc
from xmlschema import XMLSchema
from xmlschema.validators.complex_types import XsdComplexType, XsdUnion
from xmlschema.validators.simple_types import XsdAtomicRestriction, XsdAtomicBuiltin
from xmlschema.validators.elements import XsdElement
from xmlschema.validators import XsdGroup, XsdComponent
import re

from RdfGraph import graph

# Get the config parameters
import config


logger = logging.getLogger("app." + __name__)


def clean_string(description: str) -> str:
    """
    Remove newline characters, carriage returns, and leading/trailing
    whitespaces from a string. Return None if the resulting string is empty.
    """
    cleaned = description.replace("\n", " ").replace("\r", "").strip()
    if cleaned == "":
        return None
    else:
        return cleaned


def camel_case_split(s) -> str:
    """
    Transform a Camel-case string into a string with blanks to separate words
    See: https://stackoverflow.com/questions/5020906/python-convert-camel-case-to-space-delimited-using-regex-and-taking-acronyms-in
    """
    s = re.sub(r"((?<=[a-z])[A-Z]|(?<!\A)[A-Z](?=[a-z]))", r" \1", s, flags=re.VERBOSE)
    return s


def to_camel_case(s) -> str:
    """
    Transform a string into Camel-case format
    """
    s = s.replace(" ", "")
    return s[0].upper() + s[1:]


def make_component_str(component: XsdComponent) -> str:
    """
    Generate the string representating an XSD element or complex type, depending on whether this
    is a named or anonymous element. Used for logging purposes.

    Args:
        component (XsdComponent): the element or complex type

    Returns:
      formatted type name
    """

    if type(component) is XsdComplexType:
        if component.local_name is None or component.prefixed_name is None:
            component_str = f"(anonymous) {str(component)}"
        else:
            component_str = f"{graph.make_rdf_namespace(component.target_namespace)}{component.local_name} ({component.prefixed_name})"
        return component_str
    elif type(component) is XsdElement:
        if component.local_name is None or component.prefixed_name is None:
            component_str = str(component)
        else:
            component_str = f"{str(component)} {graph.make_rdf_namespace(component.target_namespace)}{component.local_name}, type: {str(component.type)}"
    else:
        raise TypeError(f"Unsupported type {type(component)}")
    return component_str


def find_first_local_name(component: XsdComponent) -> str:
    """
    Return the local name of the XSD component if any, otherwise the local name of the parent, or the parent of the parent, etc.
    Return None if no local name is found at the root component.
    """
    if component.local_name is not None:
        return component.local_name
    else:
        if component.parent is not None:
            return find_first_local_name(component.parent)
        else:
            return None


def find_first_parent_complex_type(component: XsdComponent) -> XsdComplexType:
    """
    Return the first parent of the XSD component that is an XSD complex type.
    Or return None if no parent complex type is found.
    """
    if component.parent is None:
        return None
    elif type(component.parent) is XsdComplexType:
        return component.parent
    else:
        return find_first_parent_complex_type(component.parent)


def has_element_unique_use(elem: XsdElement) -> bool:
    """
    Check if an element is used only once in the schema, or multiple times. This helps decide whether the
    corresponding property should have an rdfs:domain (single use) or not (multiple uses).

    The function looks for all the occurrences of the element in the schema.
    An occurrence may be the definition of the element `<xs:element name="ElemName"...>` or a reference
    to an element `<xs:element ref="ElemName"...>`.

    If an element is only defined (name=...) and never referenced, then it has a unique use.
    If an element is defined (name=...) globally, and referenced only once, then it has a unique use.
    In all the other cases, the element is used multiple times.

    Args:
        elem (XsdElement): the XSD element to check

    Returns:
        True if the element is used only once, False otherwise
    """
    _uri = make_element_uri(elem)
    _elem_uses = list()
    for _comp in elem.schema.iter_components():
        # Iterate over all the elements of the schema and look for one with the same URI
        if type(_comp) is XsdElement:
            if make_element_uri(_comp) == _uri:
                _elem_uses.append(_comp)

    if len(_elem_uses) <= 1:
        # The element is used once, i.e. defined (name=...) and never referenced, then it has a unique use
        return True
    elif len(_elem_uses) == 2:
        # The element is defined (name=...) globally, and referenced only once, then it has a unique use.
        if _elem_uses[0].is_global() and _elem_uses[1].ref is not None:
            return True
        if _elem_uses[1].is_global() and _elem_uses[0].ref is not None:
            return True

    return False


def make_complex_type_uri(component: XsdComplexType) -> str:
    """
    Generate a URI of an XSD complex type.
    If the complex type is named (it has a local_name), simply use this name.
    If the complex type is anonymous, recursively look for the first parent that
    has a local name and return it. Most likely that should be the parent element.

    Example:
    The type of element Throphic is an anonymous complex type:
    ```<xs:element name="Thropic" minOccurs="0">
           <xs:complexType>
               ...
    ```
    Hence we generate a new complex type named "ThropicType" that will be the range of property "hasThropic".

    Args:
        component (XsdComplexType): the XSD complex type

    Returns:
      URI as a string
    """
    _local_name = find_first_local_name(component)
    _local_name = to_camel_case(_local_name)
    _uri = graph.make_rdf_namespace(component.target_namespace) + _local_name

    # If the complex type has a local name (it is not anonymous) then we keep it as is.
    # If not, that means that find_first_local_name() has gone up to the parent element,
    # so we add "Type" to the element name to avoid name collision.
    if component.local_name is None:
        if not _uri.endswith("Type"):
            _uri += "Type"
    return _uri


def make_complex_type_label(component: XsdComplexType) -> str:
    """
    Generate a label of an XSD complex type by turning the Camel case format to string with spaces.
    If the complex type is named (it has a local name), simply use this name.
    If the complex type is anonymous, recursively look for the first parent that
    has a local name and return it. Most likely that should be the parent element.

    Example:
    type "FeedingAtomizedType" generate "Feeding Atomized Type"

    Args:
        component (XsdComplexType): the XSD complex type

    Returns:
      label
    """
    _local_name = find_first_local_name(component)
    _local_name = to_camel_case(_local_name)
    if _local_name.endswith("Type"):
        _local_name = _local_name[:-4]
    return camel_case_split(_local_name)


def make_element_uri(
    component: XsdElement, local_name: str = None, namespace: str = None
) -> str:
    """
    Create an RDF property URI from the name and namespace of an XSD element,
    where local name "Xxxx" is turned into "hasXxxx".

    If the element has no local name, the local_name parameter must be provided.
    If the element has no namespace, the namespace parameter must be provided.
    If provided, parameters local_name and namespace override those from the component if any.

    Args:
        component: the XSD component
        local_name: optional, local name to use
        namespace: optional, namespace to use

    Returns:
        RDF property URI

    Throws:
        ValueError if local name or namespace are not provided
    """

    if local_name is None and component.local_name is None:
        raise ValueError("Local name required for a component without local name")
    if local_name is None:
        local_name = component.local_name

    if namespace is None and component.target_namespace is None:
        raise ValueError("Namespace required for a component without default namespace")
    if namespace is None:
        namespace = graph.make_rdf_namespace(component.target_namespace)

    return namespace + "has" + to_camel_case(local_name)


def make_element_label(component: XsdElement, local_name: str = None) -> str:
    """
    Create an RDF property label from the name of an XSD element,
    by turning the Camel case format to string with spaces.
    Example: name "XxYy" is turned into "has Xx Yy".

    If the element has no local name, the local_name parameter must be provided.
    If provided, parameter local_name overrides the one from the component if any.

    Args:
        component: the XSD component
        local_name: optional, local name to use

    Returns:
        RDF property label

    Throws:
        ValueError if local name or namespace are not provided
    """

    if local_name is None and component.local_name is None:
        raise ValueError("Local name required for a component without local name")
    if local_name is None:
        local_name = component.local_name

    return camel_case_split("has" + to_camel_case(local_name))


def get_annotation(component: XsdComponent) -> None:
    """
    Returns the annotation of a XsdComponent, or None if empty.
    The annotation is trimed and cleaned from line breaks
    """
    if component.annotation is not None:
        _annot = clean_string(str(component.annotation))
        if _annot is not None:
            return _annot
    return None


def map_xsd_builtin_type_to_rdf(xsd_type: str) -> URIRef:
    """
    Map XSD builtin types to RDF datatypes.

    Args:
        xsd_type (str): XSD builtin type prefixed with "xs:"

    Returns:
        str: datatype from RDFlib (URIRef), or None if not recognized
    """
    _datatype = None

    # Map XSD builtin types to RDF datatypes
    match xsd_type:
        case "xs:string":
            _datatype = XSD.string
        case "xs:boolean":
            _datatype = XSD.boolean
        case "xs:decimal":
            _datatype = XSD.decimal
        case "xs:float":
            _datatype = XSD.float
        case "xs:double":
            _datatype = XSD.double
        case "xs:duration":
            _datatype = XSD.duration
        case "xs:dateTime":
            _datatype = XSD.dateTime
        case "xs:time":
            _datatype = XSD.time
        case "xs:date":
            _datatype = XSD.date
        case "xs:gYearMonth":
            _datatype = XSD.gYearMonth
        case "xs:gYear":
            _datatype = XSD.gYear
        case "xs:gMonthDay":
            _datatype = XSD.gMonthDay
        case "xs:gDay":
            _datatype = XSD.gDay
        case "xs:gMonth":
            _datatype = XSD.gMonth
        case "xs:hexBinary":
            _datatype = XSD.hexBinary
        case "xs:base64Binary":
            _datatype = XSD.base64Binary
        case "xs:anyURI":
            _datatype = XSD.anyURI
        case "xs:integer":
            _datatype = XSD.int
        case "xs:int":
            _datatype = XSD.int
        case "xs:short":
            _datatype = XSD.short
        case "xs:byte":
            _datatype = XSD.byte
        case "xs:nonNegativeInteger":
            _datatype = XSD.nonNegativeInteger
        case "xs:unsignedLong":
            _datatype = XSD.unsignedLong
        case "xs:unsignedInt":
            _datatype = XSD.unsignedInt
        case "xs:unsignedShort":
            _datatype = XSD.unsignedShort
        case "xs:unsignedByte":
            _datatype = XSD.unsignedByte
        case "xs:positiveInteger":
            _datatype = XSD.positiveInteger
        case _:
            _datatype = None

    return _datatype


def load_schema(filepath, namespace=None, local_copy_folder=None) -> XMLSchema:
    """
    Load an XML schema from a specified filepath with lax validation

    Args:
      filepath: path to the XML schema file
      namespace: default namespace of the schema. Defaults to None
      local_copy_folder: folder where to store the imported schemas. Defaults to None

    Returns:
      XMLSchema object of the schema after building, None in case of an error
    """
    try:
        _schema = XMLSchema(
            filepath, build=False, validation="lax", namespace=namespace
        )
        _schema.build()

        if local_copy_folder is not None:
            _schema.export(target=local_copy_folder, save_remote=True)

        return _schema

    except Exception as e:
        logger.error(f"An error occurred: {str(e)}")
        logger.error(format_exc())
        return None


def get_namespaces(schema: XMLSchema) -> list[tuple[str, str]]:
    """
    Retrieve the namespaces and associated prefixes declared in the XML schema, except the default namespace
    """
    return [(_prefix, _uri) for _prefix, _uri in schema.namespaces.items()]


def create_property_from_atribute(component: XsdComponent, indent="") -> None:
    """
    Create a data type property for each attribute of a component

    Args:
        component (XsdComponent): the component in which to look for attributes
        indent (str): optional, used to indent print outs

    Returns:
        None
    """
    component_str = make_component_str(component)

    # Only process the namespaces of interest
    if component.target_namespace not in config.get("namespaces_to_process"):
        logger.info(
            f"{indent}-- Ignoring element from non-managed namespace {component_str}"
        )
        return

    for _attribute in component.attributes:
        if _attribute is not None:
            # Create a property corresponding to the attribute, with a unique URI
            _local_name = find_first_local_name(component)
            _local_name = to_camel_case(_local_name + to_camel_case(str(_attribute)))
            _prop_uri = (
                graph.make_rdf_namespace(component.target_namespace) + "has" + _local_name
            )

            _class = camel_case_split(find_first_local_name(component)).lower()
            graph.add_datatype_property(_prop_uri, label=f"has {_class}'s {str(_attribute)}")

            _class = make_complex_type_uri(component)
            logger.debug(
                f"{indent}| Making datatype property (1) {_prop_uri} for attribute '{str(_attribute)}' of {component_str}"
            )
            graph.add_property_domain_range(_prop_uri, domain=_class)


def process_complex_type(component: XsdComplexType, indent="") -> str:
    """
    Recursively process the content of an XSD complex type.
    Only processes elements whithin the namespaces mentioned in
    config param 'namespaces_to_process'.

    Args:
        component (XsdComplexType): the xsd type to process
        indent (str): optional, used to indent print outs

    Returns:
        str: URI of the complex type created or None
    """
    component_str = make_component_str(component)

    _class = None

    # Only process the namespaces of interest
    if component.target_namespace not in config.get("namespaces_to_process"):
        logger.info(indent + f"-- Ignoring complex type {component_str}")
        return None

    logger.info(indent + f"┌ Processing complex type {component_str}")
    _annotation = get_annotation(component)
    if _annotation is not None:
        logger.debug(f'{indent}| Annotation: "{_annotation[:70]}')

    # -------------------------------------------------------
    # --- Start checking the possible cases
    # -------------------------------------------------------

    # --- Process the optional attributes of the type
    create_property_from_atribute(component, indent)

    # --- Complex type extends a builtin type: no type is created,
    #     instead the parent property will be a datatype property with builtin type as range
    if component.is_extension():
        if type(component.content) is XsdAtomicBuiltin:
            pass
        else:
            logger.warning(
                f"{indent}|  -- Not supported: complex type ({component_str}) is an extension of another complex type"
            )

    # --- Case of an xs:complexType containing only an xs:simpleContent
    elif component.has_simple_content():
        logger.warning(
            f"{indent}|  -- Complex type ({component_str}) with simple content should be managed at the parent level"
        )

    # Create the class for that complex type
    else:
        _class = make_complex_type_uri(component)
        graph.add_class(
            _class, label=make_complex_type_label(component), description=_annotation
        )

        for _component in component.content.iter_model():
            try:
                if type(_component) is XsdElement:
                    process_element(_component, indent + "| ")
                elif type(_component) is XsdGroup:
                    process_group(_component, indent + "| ")
                else:
                    logger.warning(
                        indent + f"  -- Non-managed type component {str(_component)}"
                    )
            except Exception as e:
                logger.warning(
                    f"-- Error while processing component {str(_component)}, parent {str(_component.parent)}: {str(e)}"
                )
                logger.error(format_exc())

    logger.info(indent + "└ Completed processing complex type " + component_str)
    return _class


def process_simple_type_restriction(component: XsdAtomicRestriction, indent="") -> str:
    """
    Create a class for an enumeration: <xs:simpleType><xs:restriction base="xs:string"><xs:enumeration value=...

    Args:
        component (XsdAtomicRestriction): the xsd type to process
        indent (str): optional, used to indent print outs

    Returns:
        str: URI of the type created
    """

    component_str = str(component)
    _class = None

    # Only process the namespaces of interest
    if component.target_namespace not in config.get("namespaces_to_process"):
        logger.info(indent + f"-- Ignoring restriction type {component_str}")
        return None

    if component.enumeration is None:
        logger.info(
            indent + f"-- Ignoring restriction type without enum values {component_str}"
        )
        return None

    logger.info(indent + f"┌ Processing restriction type {component_str}")
    _annotation = get_annotation(component)
    if _annotation is not None:
        logger.debug(f'{indent}| Annotation: "{_annotation[:70]}')

    if component.local_name is not None:
        # If the type is named then simply use its local name.
        _local_name = to_camel_case(component.local_name)
    else:
        # Otherwise, use the local name of the first parent element and add EnumType for desambiguation
        _local_name = find_first_local_name(component)
        _local_name = to_camel_case(_local_name) + "EnumType"

    _class = graph.make_rdf_namespace(component.target_namespace) + _local_name
    graph.add_class(
        _class,
        label=f"Enum values for {to_camel_case(find_first_local_name(component))}",
        description=_annotation,
    )

    # The enum values are the members of a new class defined as owl:oneOf
    _enum_values = [str(_enum) for _enum in component.enumeration]
    graph.add_oneof_class_members(
        _class,
        members=_enum_values,
        enum_type=component.base_type.prefixed_name,
    )
    logger.debug(f"{indent}|  Enum values: {_enum_values}")

    logger.info(indent + "└ Completed processing restriction type " + component_str)
    return _class


def process_group(component: XsdGroup, indent="") -> None:
    """
    Process the content of an XsdGroup, i.e. a sequence, choice...

    Args:
        component (XsdGroup): the xsd group to process
        indent (str): optional, used to indent print outs
    """
    indent = f"{indent}| "
    logger.debug(indent + str(component))
    _annotation = get_annotation(component)
    if _annotation is not None:
        logger.debug(f'{indent}| Annotation: "{_annotation[:70]}')

    for _component in component.iter_model():
        if type(_component) is XsdElement:
            process_element(_component, indent)
        elif type(_component) is XsdGroup:
            process_group(_component, indent)
        else:
            logger.warning(indent + f"Non-managed group {str(_component)}")


def process_element(component: XsdElement, indent="") -> None:
    """
    Process the content of an XSD element, either:
    - globally-defined, named, typed element, i.e. an element retrieved
    from schema.iter_globals() and defined as: <xs:element name="ElementName" type="ElementType"/>
    - non-global, named element, i.e. defined whithin the scope of another element, typically a complex type.

    Each element entails the creation of an OWL datatype or object property.
    The range of the property is set depending on the element type.
    An element whose type is an enum entails the creation of a class with the enum values as individuals.

    Args:
        component (XsdElement): the XSD element to process
        indent (str): optional, used to indent print outs
    """
    indent = f"{indent}| "
    component_str = make_component_str(component)

    # Only process the namespaces of interest
    if component.target_namespace not in config.get("namespaces_to_process"):
        logger.info(
            f"{indent}-- Ignoring element from non-managed namespace {component_str}"
        )
        return

    # Get the optional annotation of the element
    _annotation = get_annotation(component)
    if _annotation is not None:
        logger.debug(f'{indent}| Annotation: "{_annotation[:70]}')

    # Make the URI and label of the property to be be created
    _prop_uri = make_element_uri(component)
    _prop_label = make_element_label(component).lower()

    # Find the parent complex type to be used as the rdfs:domain of the property to be created.
    # This must be done for elements (referenced or named) that are embedded in a complex type
    # because this complex type is the parent type we are looking for.
    # Whereas a global element has no parent type by definition.
    _parent_complex_type = find_first_parent_complex_type(component)
    if _parent_complex_type is not None and has_element_unique_use(component):
        _parent_uri = make_complex_type_uri(_parent_complex_type)
        graph.add_property_domain_range(_prop_uri, domain=_parent_uri)

    # Only process named elements, i.e. defined as: <xs:element name="Element" type="ElementType"/>.
    if component.ref is not None:
        logger.debug(f"{indent}-- Ignoring referenced {str(component)}")
        return

    logger.debug(f"{indent}Processing element {component_str}")

    # -------------------------------------------------------
    # --- Start checking the possible forms of an element
    # -------------------------------------------------------

    # --- Case of an XsdAtomicBuiltin e.g. with type "xs:string", "xs:float" etc.
    if type(component.type) is XsdAtomicBuiltin:
        graph.add_datatype_property(
            _prop_uri, label=_prop_label, description=_annotation
        )
        _datatype = map_xsd_builtin_type_to_rdf(component.type.prefixed_name)
        if _datatype is not None:
            logger.debug(
                f"{indent}| Making datatype property (2) {_prop_uri} for {component_str}"
            )
            graph.add_property_domain_range(_prop_uri, range=_datatype)
        else:
            logger.warning(f"{indent}| Non-managed builtin type {component_str}")

    # --- Case of an enumeration: <xs:simpleType><xs:restriction base="xs:string"><xs:enumeration value=...
    elif type(component.type) is XsdAtomicRestriction:
        _type_local_name = component.type.local_name
        if _type_local_name is not None:
            # This enum type is defined globally and just named here: <xsd:element name="elem" type="EnumType">
            # It must be processed separately, so we just keep the corresponding class name
            _class_enum = graph.make_rdf_namespace(
                component.target_namespace
            ) + to_camel_case(_type_local_name)
        else:
            # This enum type is defined locally, so the enum values become the members of a new class defined as owl:oneOf
            _class_enum = process_simple_type_restriction(component.type, f"{indent}| ")

        # The new property has as range the enumerated class above
        logger.debug(
            f"{indent}| Making object property {_prop_uri} for {component_str}"
        )
        graph.add_object_property(_prop_uri, label=_prop_label, description=_annotation)
        graph.add_property_domain_range(_prop_uri, range=_class_enum)

    # --- Other cases where the element has a complex type
    elif type(component.type) is XsdComplexType:

        # --- Process the optional attributes of the type
        create_property_from_atribute(component.type, indent)

        # --- Assumption: a complex element of type xs:anyType is used for NL notes -> datatype property
        if component.type.prefixed_name == "xs:anyType":
            logger.debug(
                f"{indent}| Making datatype property (3) {_prop_uri} for {component_str}"
            )
            graph.add_datatype_property(
                _prop_uri, label=_prop_label, description=_annotation
            )

        # --- Complex type extends a builtin type -> datatype property
        elif (
            component.type.is_extension()
            and type(component.type.content) is XsdAtomicBuiltin
        ):
            graph.add_datatype_property(
                _prop_uri, label=_prop_label, description=_annotation
            )
            _datatype = map_xsd_builtin_type_to_rdf(
                component.type.content.prefixed_name
            )
            if _datatype is not None:
                logger.debug(
                    f"{indent}| Making datatype property (4) {_prop_uri} for {component_str}"
                )
                graph.add_property_domain_range(_prop_uri, range=_datatype)
            else:
                logger.warning(
                    f"{indent}| Non-managed builtin type for {component_str}"
                )

        # --- Finally, case where the xs:complexType is "really" a complex type
        else:
            logger.debug(
                f"{indent}| Making object property {_prop_uri} for {component_str}"
            )
            graph.add_object_property(
                _prop_uri, label=_prop_label, description=_annotation
            )
            # Case of a named complex type
            if component.type.local_name is not None:
                graph.add_property_domain_range(
                    _prop_uri,
                    range=graph.make_rdf_namespace(component.type.target_namespace)
                    + component.type.local_name,
                )
            # Case of an anonymous complex type
            else:
                _class = process_complex_type(component.type, f"{indent}| ")
                if _class is not None:
                    graph.add_property_domain_range(_prop_uri, range=_class)

    # --- Case of an XsdUnion: in a first approach, these are considered as simple literal data types
    elif type(component.type) is XsdUnion:
        graph.add_datatype_property(
            _prop_uri, label=_prop_label, description=_annotation
        )

    else:
        logger.warning(f"{indent}-- Non-managed element {component_str}")
