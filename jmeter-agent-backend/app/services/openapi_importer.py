"""OpenAPI/Swagger auto-import — parse spec → create project + test plans.

Supports local references, scalar parameters and JSON or form object examples/defaults.
Authentication credentials and unsupported serialization require manual setup.
"""

import json
import logging
import re
import asyncio
import math
import os
import zlib
from typing import Optional
from dataclasses import dataclass, field
from urllib.parse import urlsplit, urljoin, quote

import httpx
from app.core.config import settings

logger = logging.getLogger(__name__)
MAX_SPEC_BYTES = 5 * 1024 * 1024
SPEC_FETCH_TIMEOUT_SECONDS = 30
MAX_SPEC_REDIRECTS = 5
MAX_SPEC_NODES = 50000
MAX_SPEC_DEPTH = 64
MAX_SPEC_GZIP_MEMBERS = 64


@dataclass
class ParsedEndpoint:
    name: str
    method: str
    path: str
    description: str = ""
    parameters: list = field(default_factory=list)
    request_body: Optional[dict] = None
    responses: dict = field(default_factory=dict)
    security: list = field(default_factory=list)
    tags: list = field(default_factory=list)


@dataclass
class ParsedSpec:
    title: str
    version: str = ""
    description: str = ""
    base_url: str = ""
    endpoints: list[ParsedEndpoint] = field(default_factory=list)
    security_schemes: dict = field(default_factory=dict)
    auth_type: str = ""  # "bearer" | "basic" | "apikey" | "oauth2" | "none"


@dataclass
class ImportResult:
    success: bool
    project_id: str = ""
    project_name: str = ""
    endpoints_found: int = 0
    auth_detected: str = ""
    jmx_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def model_dump(self) -> dict:
        return {
            "success": self.success,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "endpoints_found": self.endpoints_found,
            "auth_detected": self.auth_detected,
            "jmx_paths": self.jmx_paths,
            "warnings": self.warnings,
            "error": self.error,
        }


class OpenAPIImporter:
    """Parse OpenAPI/Swagger specs and auto-generate project + test plans."""

    @staticmethod
    def _check_document_budget(document):
        """Bound traversal of JSON/YAML trees, including cyclic/shared aliases."""
        pending = [(document, 0, False)]
        active = set()
        nodes = size = 0
        while pending:
            value, depth, leaving = pending.pop()
            if leaving:
                active.remove(id(value))
                continue
            nodes += 1
            if nodes > MAX_SPEC_NODES or depth > MAX_SPEC_DEPTH:
                raise ValueError('Spec structure exceeds node/depth budget')
            if isinstance(value, (dict, list)):
                if id(value) in active:
                    raise ValueError('Spec contains cyclic aliases')
                if len(value) > MAX_SPEC_NODES:
                    raise ValueError('Spec structure exceeds node budget')
                active.add(id(value))
                pending.append((value, depth, True))
                children = list(value.values()) if isinstance(value, dict) else value
                pending.extend((child, depth + 1, False) for child in children)
                if isinstance(value, dict):
                    pending.extend((key, depth + 1, False) for key in value)
            elif isinstance(value, str):
                if len(value) > MAX_SPEC_BYTES:
                    raise ValueError('Spec exceeds text size limit')
                size += len(value.encode('utf-8'))
                if size > MAX_SPEC_BYTES:
                    raise ValueError('Spec exceeds text size limit')
            elif value is not None and not isinstance(value, (bool, int, float)):
                raise ValueError('Spec contains unsupported non-JSON values')
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError('Spec contains non-finite numbers')

    @staticmethod
    def _object(value, label):
        if not isinstance(value, dict):
            raise ValueError(f'{label} must be an object')
        return value

    @staticmethod
    def _spec_url(url: str) -> httpx.URL:
        parsed = httpx.URL(url)
        if parsed.scheme not in ('http', 'https') or not parsed.host or parsed.userinfo:
            raise ValueError('Spec URL must use HTTP(S), a host, and no embedded credentials')
        return parsed

    @staticmethod
    def _decode_spec_body(raw: bytes, encoding: str) -> bytes:
        """Decode bounded wire data without an unbounded HTTP content decoder."""
        if encoding in ('', 'identity'):
            return raw
        result = bytearray()
        members = 0
        while True:
            members += 1
            if members > MAX_SPEC_GZIP_MEMBERS:
                raise ValueError('Spec exceeds compressed member limit')
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS if encoding == 'gzip' else zlib.MAX_WBITS)
            allowance = MAX_SPEC_BYTES - len(result) + 1  # Never pass zero (unlimited) to zlib.
            try:
                try:
                    decoded = decoder.decompress(raw, allowance)
                except zlib.error:
                    if encoding != 'deflate':
                        raise
                    # Preserve HTTP servers that emit raw rather than zlib-wrapped deflate.
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    decoded = decoder.decompress(raw, allowance)
            except zlib.error as error:
                raise ValueError('Invalid compressed spec body') from error
            if len(result) + len(decoded) > MAX_SPEC_BYTES:
                raise ValueError('Spec exceeds decoded size limit')
            result.extend(decoded)
            if not decoder.eof:
                raise ValueError('Truncated compressed spec body')
            raw = decoder.unused_data
            if encoding == 'gzip':
                raw = raw.lstrip(b'\x00')  # Gzip allows padding between/after members.
            if not raw:
                return bytes(result)
            if encoding != 'gzip':
                raise ValueError('Unexpected data after compressed spec body')

    async def _fetch_spec(self, url: str) -> tuple[str, str]:
        current = self._spec_url(url)
        origin = (current.scheme, current.host, current.port)
        async with httpx.AsyncClient(timeout=SPEC_FETCH_TIMEOUT_SECONDS, follow_redirects=False) as client:
            for redirects in range(MAX_SPEC_REDIRECTS + 1):
                async with client.stream('GET', current, headers={'Accept-Encoding': 'gzip, deflate, identity'}) as response:
                    if response.is_redirect:
                        if redirects == MAX_SPEC_REDIRECTS or not response.headers.get('location'):
                            raise ValueError('Spec redirect limit reached or Location missing')
                        target = self._spec_url(str(current.join(response.headers['location'])))
                        if (target.scheme, target.host, target.port) != origin:
                            raise ValueError('Cross-origin spec redirect blocked; review and supply the final URL explicitly')
                        current = target
                        continue
                    response.raise_for_status()
                    length = response.headers.get('content-length')
                    if length and int(length) > MAX_SPEC_BYTES:
                        raise ValueError('Spec exceeds download size limit')
                    encoding = response.headers.get('content-encoding', '').strip().lower()
                    if encoding not in ('', 'identity', 'gzip', 'deflate'):
                        raise ValueError('Unsupported spec Content-Encoding; use identity, gzip or deflate')
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        if len(body) + len(chunk) > MAX_SPEC_BYTES:
                            raise ValueError('Spec exceeds download size limit')
                        body.extend(chunk)
                    decoded = await asyncio.to_thread(self._decode_spec_body, bytes(body), encoding)
                    return decoded.decode(response.encoding or 'utf-8'), str(current)
        raise ValueError('Spec redirect limit reached')

    async def import_from_url(self, url: str) -> ImportResult:
        """Fetch an OpenAPI spec from a URL and import it."""
        try:
            body, final_url = await asyncio.wait_for(self._fetch_spec(url), timeout=SPEC_FETCH_TIMEOUT_SECONDS)
        except ValueError as e:
            return ImportResult(success=False, error=f"Failed to fetch spec: {e}")
        except Exception as e:
            # HTTP errors include full URLs, which can contain query credentials.
            return ImportResult(success=False, error=f"Failed to fetch spec: {type(e).__name__}")

        # Parse YAML if needed
        if url.endswith(".yaml") or url.endswith(".yml") or not body.strip().startswith("{"):
            try:
                import yaml
                spec = await asyncio.to_thread(yaml.safe_load, body)
            except Exception:
                # Try JSON anyway
                try:
                    spec = await asyncio.to_thread(json.loads, body)
                except Exception:
                    return ImportResult(success=False, error="Cannot parse spec as JSON or YAML. Install pyyaml for YAML support.")
        else:
            try:
                spec = await asyncio.to_thread(json.loads, body)
            except (ValueError, RecursionError):
                return ImportResult(success=False, error="Invalid JSON in spec")

        return await self.import_from_dict(spec, final_url)

    async def import_from_dict(self, spec: dict, source_url: str = "") -> ImportResult:
        """Parse an OpenAPI spec dict and create a project with a checked baseline plan.

        Nothing runs: the caller starts a run when it wants one, through the usual approvals.
        """
        try:
            parsed = await asyncio.to_thread(self._parse_spec, spec, source_url)
        except (ValueError, TypeError, KeyError) as exc:
            return ImportResult(success=False, error=f"Cannot import spec: {exc}")

        if not parsed.endpoints:
            return ImportResult(
                success=False, error="No endpoints found in the spec",
            )

        # Create project
        import uuid
        project_id = str(uuid.uuid4())[:8]

        from app.services.storage import store
        from app.models.schemas import ProjectInfo, ProjectEnvironment
        from datetime import datetime

        from app.services.execution_lifecycle import default_environments
        environment = ProjectEnvironment(base_url=parsed.base_url)
        project = ProjectInfo(
            id=project_id,
            name=parsed.title,
            description=parsed.description or f"Imported from {source_url}",
            environment=environment,
            environments=default_environments(environment),
            created_at=datetime.now(),
        )

        # Generate test plans
        from app.services.jmeter.jmx_generator import JMXGenerator
        from app.models.schemas import TestConfig

        generator = JMXGenerator()
        jmx_paths = []
        warnings = []
        project_created = False
        plan_id = None

        # Generate baseline JMX
        try:
            eps = [self._api_endpoint(ep, parsed, warnings) for ep in parsed.endpoints]
            config = TestConfig(test_type="baseline", concurrent_users=10, duration_seconds=300)
            jmx = generator.generate_basic_jmx(eps, config, parsed.base_url, f"{parsed.title} Baseline")
            # The same checks a plan written by the model passes, before anything is created.
            from app.services.jmeter import jmx_lint
            # An auth token is left for the user to supply (a warning says so); anything else must be right.
            failures, issues = await asyncio.to_thread(jmx_lint.check, jmx, ('undefined_variable',))
            if failures:
                raise ValueError("the generated plan did not pass its checks: " + jmx_lint.describe(failures))
            warnings.extend(issue['message'] for issue in issues if issue['severity'] == 'warning')
            from app.services.db_store import db_store
            from app.services.execution_lifecycle import execution_lifecycle
            await db_store.create_project({
                "id": project_id, "name": project.name, "description": project.description,
                "base_url": parsed.base_url, "environment_data": project.environment.model_dump(),
                "environments_data": project.environments.model_dump(context={'reveal_secrets': True}),
                "created_at": project.created_at})
            project_created = True
            plan_id = uuid.uuid4().hex
            store.set_project(project_id, project)
            jmx_path = generator.save_jmx(jmx, name=f"{parsed.title} Baseline", project_id=project_id,
                                          plan_id=plan_id)
            plan = await execution_lifecycle.register_path(jmx_path, project_id=project_id,
                name=f"{parsed.title} Baseline", test_config=config, endpoints_count=len(eps))
            jmx_paths.append(jmx_path)
        except Exception as e:
            cleanup_errors = []
            if project_created:
                expected_path = (os.path.join(settings.TEST_PLANS_DIR, project_id, f'{plan_id}.jmx')
                                 if plan_id else None)
                if expected_path:
                    try:
                        if os.path.isfile(expected_path):
                            os.remove(expected_path)
                        project_dir = os.path.dirname(expected_path)
                        if os.path.isdir(project_dir) and not os.listdir(project_dir):
                            os.rmdir(project_dir)
                    except OSError:
                        cleanup_errors.append('generated JMX file')
                try:
                    if plan_id:
                        store.delete_test_plan(plan_id)
                    store.delete_project(project_id)
                    await db_store.delete_project(project_id)
                except Exception:
                    cleanup_errors.append('project registration')
            if cleanup_errors:
                warnings.append('Import rollback incomplete: ' + ', '.join(cleanup_errors))
            return ImportResult(success=False, project_id=project_id, project_name=parsed.title,
                endpoints_found=len(parsed.endpoints), auth_detected=parsed.auth_type,
                warnings=warnings, error=f"Baseline import failed: {e}")

        return ImportResult(
            success=True,
            project_id=project_id,
            project_name=parsed.title,
            endpoints_found=len(parsed.endpoints),
            auth_detected=parsed.auth_type,
            jmx_paths=jmx_paths,
            warnings=warnings,
        )

    def _parse_spec(self, spec: dict, source_url: str) -> ParsedSpec:
        """Parse an OpenAPI 3.x or Swagger 2.x spec into a ParsedSpec."""
        self._check_document_budget(spec)
        ref_budget = [MAX_SPEC_NODES]
        if not isinstance(spec, dict) or not (str(spec.get("openapi", "")).startswith("3.") or spec.get("swagger") == "2.0"):
            raise ValueError("Expected an OpenAPI 3.x or Swagger 2.0 object")
        # OpenAPI 3.x
        openapi = spec.get("openapi", "")
        info = self._object(spec.get("info", {}), 'info')
        title = info.get("title", "Imported API")
        version = info.get("version", "")
        description = info.get("description", "")

        # Determine base URL
        base_url = ""
        servers = spec.get("servers", [])
        if not isinstance(servers, list):
            raise ValueError('servers must be an array')
        for server in servers:
            self._object(server, 'server')
        if servers and servers[0].get("url"):
            base_url = servers[0]["url"]
            for name, definition in self._object(servers[0].get("variables", {}), 'server variables').items():
                self._object(definition, 'server variable')
                if "default" not in definition:
                    raise ValueError(f"Server variable {name} needs a default")
                base_url = base_url.replace("{" + name + "}", str(definition["default"]))
            base_url = urljoin(source_url, base_url)

        if spec.get("swagger") == "2.0" and spec.get("host"):
            scheme = (spec.get("schemes") or [urlsplit(source_url).scheme or "https"])[0]
            base_url = f"{scheme}://{spec['host']}{spec.get('basePath', '')}"

        if source_url and not base_url:
            # Extract from source URL
            from urllib.parse import urlparse
            parsed_url = urlparse(source_url)
            base_url = f"{parsed_url.scheme}://{parsed_url.netloc}"
            if spec.get("swagger") == "2.0":
                base_url += spec.get("basePath", "")

        # Security schemes
        security_schemes = {}
        components = self._object(spec.get("components", {}), 'components')
        sec_schemes = components.get("securitySchemes", {})
        # Also Swagger 2.x
        if not sec_schemes:
            sec_schemes = spec.get("securityDefinitions", {})

        auth_type = "none"
        for name, scheme in self._object(sec_schemes, 'securitySchemes').items():
            scheme = self._object(self._resolve_refs(scheme, spec, budget=ref_budget), 'security scheme')
            stype = scheme.get("type", "")
            if not isinstance(stype, str):
                raise ValueError('Security scheme type must be a string')
            stype = stype.lower()
            if stype == "http":
                stype = scheme.get("scheme", "")
                if not isinstance(stype, str):
                    raise ValueError('HTTP security scheme must be a string')
                stype = stype.lower()
            if "bearer" in stype or "jwt" in stype:
                auth_type = "bearer"
            elif "basic" in stype:
                auth_type = "basic"
            elif "apikey" in stype:
                auth_type = "apikey"
            elif "oauth" in stype:
                auth_type = "oauth2"
            security_schemes[name] = scheme

        # Parse paths
        endpoints = []
        paths = self._object(spec.get("paths", {}), 'paths')

        for path, methods in paths.items():
            methods = self._resolve_refs(methods, spec, budget=ref_budget)
            if not isinstance(methods, dict):
                continue
            for method in ("get", "post", "put", "delete", "patch", "options", "head"):
                operation = methods.get(method)
                if operation is None:
                    continue
                self._object(operation, 'operation')
                if operation.get("servers") or methods.get("servers"):
                    raise ValueError(f"{method.upper()} {path}: per-operation/path server overrides are unsupported")
                parameters = {}
                for owner in (methods, operation):
                    declarations = owner.get('parameters', [])
                    if not isinstance(declarations, list):
                        raise ValueError('parameters must be an array')
                    for parameter in declarations:
                        self._object(parameter, 'parameter')
                        parameters[(parameter.get('in'), parameter.get('name'))] = parameter
                request_body = operation.get("requestBody")
                body_parameters = [p for p in parameters.values() if p.get("in") == "body"]
                if body_parameters:
                    media = (operation.get("consumes") or spec.get("consumes") or ["application/json"])[0]
                    request_body = {"content": {media: {"schema": body_parameters[0].get("schema", {})}}}
                ep = ParsedEndpoint(
                    name=operation.get("summary", f"{method.upper()} {path}"),
                    method=method.upper(),
                    path=path,
                    description=operation.get("description", ""),
                    parameters=list(parameters.values()),
                    request_body=request_body,
                    responses=operation.get("responses", {}),
                    security=operation.get("security", spec.get("security", [])),
                    tags=operation.get("tags", []),
                )
                endpoints.append(ep)

        return ParsedSpec(
            title=title,
            version=version,
            description=description,
            base_url=base_url,
            endpoints=endpoints,
            security_schemes=security_schemes,
            auth_type=auth_type,
        )

    def _resolve_refs(self, node, document, stack=(), *, budget=None, depth=0):
        if budget is None:
            budget = [MAX_SPEC_NODES]
        budget[0] -= 1
        if budget[0] < 0 or depth > MAX_SPEC_DEPTH:
            raise ValueError('Expanded $ref exceeds node/depth budget')
        if len(stack) > 32:
            raise ValueError("$ref nesting exceeds 32 levels")
        if isinstance(node, list):
            return [self._resolve_refs(value, document, stack, budget=budget, depth=depth + 1) for value in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            reference = node["$ref"]
            if not isinstance(reference, str) or not reference.startswith("#/"):
                raise ValueError(f"External $ref is not supported; inline it first: {reference}")
            if reference in stack:
                raise ValueError(f"Cyclic $ref needs an explicit finite example: {reference}")
            target = document
            try:
                for component in reference[2:].split("/"):
                    target = target[component.replace("~1", "/").replace("~0", "~")]
            except (KeyError, TypeError) as exc:
                raise ValueError(f"Unresolved $ref: {reference}") from exc
            return self._resolve_refs(target, document, stack + (reference,), budget=budget, depth=depth + 1)
        return {key: self._resolve_refs(value, document, stack, budget=budget, depth=depth + 1)
                for key, value in node.items()}

    def _example(self, node):
        for key in ("example", "default"):
            if key in node:
                return node[key]
        if node.get("examples"):
            examples = node["examples"]
            value = next(iter(examples.values())) if isinstance(examples, dict) else examples[0]
            return value.get("value", value) if isinstance(value, dict) else value
        if "schema" in node:
            return self._example(node["schema"])
        if node.get("enum"):
            return node["enum"][0]
        if node.get("type") == "object" or "properties" in node:
            return {name: self._example(value) for name, value in node.get("properties", {}).items()}
        raise ValueError("Provide an example/default for the parameter or request body")

    def _api_endpoint(self, ep, parsed, warnings, templated_path=False):
        """The request a plan sends. With templated_path, a path parameter without an example stays
        {name} for the plan to bind (an id the flow creates), and the rest of the request is kept."""
        from app.models.schemas import APIEndpoint
        path, headers, query = ep.path, {}, {}
        for parameter in ep.parameters:
            location, name = parameter.get("in"), parameter.get("name")
            if location == "body":
                continue
            if location not in {"path", "query", "header"}:
                raise ValueError(f"{ep.path}: unsupported parameter location {location}; edit the plan manually")
            try:
                value = self._example(parameter)
            except ValueError:
                if location == "path" and templated_path:
                    warnings.append(f"{ep.path}: path parameter {name} has no example; bind it to a value the flow extracts")
                    continue
                if parameter.get("required") or location == "path":
                    raise ValueError(f"{ep.path}: required parameter {name} needs an example/default")
                warnings.append(f"{ep.path}: optional parameter {name} omitted; no example/default")
                continue
            if isinstance(value, (dict, list)):
                raise ValueError(f"{ep.path}: parameter {name} requires unsupported structured serialization")
            if location == "path":
                path = path.replace("{" + name + "}", quote(str(value), safe=""))
            elif location == "query":
                query[name] = value
            else:
                headers[name] = str(value)
        if re.search(r"\{[^{}]+\}", path) and not templated_path:
            raise ValueError(f"Unresolved path parameter in {path}")
        body, content_type = None, "application/json"
        if ep.request_body:
            content = ep.request_body.get("content", {})
            json_types = [key for key in content if key == "application/json" or key.endswith("+json")]
            # A login form: sending it as JSON was refused (415) before any request got a token.
            form_types = [key for key in content if key == "application/x-www-form-urlencoded"]
            if not json_types and not form_types:
                raise ValueError(f"{ep.path}: only JSON and form (application/x-www-form-urlencoded) bodies are supported")
            content_type = (json_types or form_types)[0]
            body = self._example(content[content_type])
            if not isinstance(body, dict):
                raise ValueError(f"{ep.path}: the request body must be an object in this importer")
        if ep.security and not any(not requirement for requirement in ep.security):
            # Select the first documented OR alternative; schemes within it are AND.
            for name in ep.security[0]:
                scheme = parsed.security_schemes.get(name, {})
                kind = scheme.get("scheme", scheme.get("type", "")).lower()
                if kind == "bearer":
                    headers["Authorization"] = "Bearer ${auth_token}"
                    warnings.append(f"{ep.path}: supply auth_token; token acquisition is not inferred")
                else:
                    raise ValueError(f"{ep.path}: security scheme {name} ({kind}) requires manual configuration")
        success_codes = sorted(int(code) for code in ep.responses if str(code).isdigit() and 200 <= int(code) < 300)
        return APIEndpoint(name=ep.name, method=ep.method, path=path, description=ep.description,
            headers=headers, query_params=query, body=body, content_type=content_type,
            expected_status=success_codes[0] if success_codes else 200)


RESPONSE_FIELDS = 20  # properties listed per object of a documented response
RESPONSE_DEPTH = 3


def _fields(schema, depth=0):
    """A documented JSON shape, small: {"token": "string", "items": [{"id": "integer"}]}."""
    if not isinstance(schema, dict):
        return 'any'
    if 'example' in schema and depth == 0:
        return schema['example']
    for key in ('allOf', 'oneOf', 'anyOf'):
        if isinstance(schema.get(key), list) and schema[key]:
            merged = {}
            for part in schema[key]:
                shape = _fields(part, depth)
                if isinstance(shape, dict):
                    merged.update(shape)
            return merged or _fields(schema[key][0], depth)
    if schema.get('type') == 'array' or 'items' in schema:
        return [_fields(schema.get('items'), depth + 1)] if depth < RESPONSE_DEPTH else ['...']
    if schema.get('type') == 'object' or 'properties' in schema:
        if depth >= RESPONSE_DEPTH:
            return 'object'
        names = list(schema.get('properties') or {})[:RESPONSE_FIELDS]
        return {name: _fields(schema['properties'][name], depth + 1) for name in names}
    kind = schema.get('type') or 'any'
    # "code: integer" alone hid that 0 is success, so checks only tested that the field exists.
    if schema.get('enum'):
        kind += ' (one of ' + ', '.join(map(str, schema['enum'][:10])) + ')'
    if schema.get('description'):
        kind += ' — ' + str(schema['description'])[:120]
    return kind


def _response_shape(importer: 'OpenAPIImporter', ep: ParsedEndpoint, spec: dict) -> Optional[dict]:
    """What the success response of an endpoint carries, so plans extract and check real fields.

    Without it the model guessed: a boundary extractor of 'token":"' missed '{"token": "..."}'
    and every request needing the token failed.
    """
    codes = sorted(code for code in ep.responses if str(code).isdigit() and 200 <= int(code) < 300)
    if not codes:
        return None
    try:
        response = importer._resolve_refs(ep.responses[codes[0]], spec)
    except ValueError:
        return None
    if not isinstance(response, dict):
        return None
    content = response.get('content')
    if content is None and 'schema' in response:  # Swagger 2
        content = {'application/json': {'schema': response['schema'], 'example': (response.get('examples') or {}).get('application/json')}}
    if not isinstance(content, dict):
        return None
    media = next((key for key in content if key == 'application/json' or key.endswith('+json')), None)
    if media is None or not isinstance(content[media], dict):
        return None
    body = content[media]
    example = body.get('example')
    if example is None and isinstance(body.get('examples'), dict) and body['examples']:
        first = next(iter(body['examples'].values()))
        example = first.get('value') if isinstance(first, dict) else None
    shape = {'status': int(codes[0]), 'json': example if example is not None else _fields(body.get('schema'))}
    return shape if shape['json'] not in (None, 'any', {}) else None


def endpoints_from_document(text: str) -> Optional[dict]:
    """The endpoints of an OpenAPI/Swagger document, parsed by code; None for any other text.

    A model that reads a spec can skip or invent endpoints; the parsed list cannot.
    """
    stripped = (text or '').strip()
    if not stripped or len(stripped.encode('utf-8')) > MAX_SPEC_BYTES:
        return None
    # JSON is cheap to try; YAML only when a top-level openapi/swagger key says it is a spec.
    if not stripped.startswith('{') and not re.search(r'''^["']?(openapi|swagger)["']?\s*:''', stripped, re.MULTILINE):
        return None
    try:
        if stripped.startswith('{'):
            spec = json.loads(stripped)
        else:
            import yaml
            spec = yaml.safe_load(stripped)
        importer = OpenAPIImporter()
        parsed = importer._parse_spec(spec, '')
    except Exception:
        return None
    if not parsed.endpoints:
        return None
    endpoints, notes = [], []
    for ep in parsed.endpoints:
        item = {'name': ep.name, 'method': ep.method, 'path': ep.path}
        try:
            own: list[str] = []
            api = importer._api_endpoint(ep, parsed, own, templated_path=True)
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            # Still listed: the model fills in what the spec leaves out, from the parameters.
            item['parameters'] = [{key: parameter[key] for key in ('name', 'in', 'required') if key in parameter}
                                  for parameter in ep.parameters]
            item['note'] = str(error)
        else:
            unbound = [note for note in own if ': path parameter ' in note]
            notes.extend(note for note in own if note not in unbound)
            if unbound:
                # On the endpoint, where the model writes its binding.
                item['parameters'] = [{key: parameter[key] for key in ('name', 'in', 'required') if key in parameter}
                                      for parameter in ep.parameters if parameter.get('in') == 'path']
                item['note'] = '; '.join(unbound)
            if api.path != ep.path:
                item.update(path=api.path, documented_path=ep.path)
            item.update({key: value for key, value in (
                ('headers', api.headers), ('query_params', api.query_params), ('body', api.body),
                ('content_type', api.content_type if api.body is not None else None),
                ('expected_status', api.expected_status)) if value})
        response = _response_shape(importer, ep, spec)
        if response:
            item['response'] = response
        if ep.description:
            item['description'] = str(ep.description)[:200]
        if ep.tags:
            item['tags'] = ep.tags
        endpoints.append(item)
    return {'title': parsed.title, 'version': parsed.version, 'base_url': parsed.base_url,
            # Conventions every endpoint shares (business codes, required headers) live here.
            **({'description': str(parsed.description)[:2000]} if parsed.description else {}),
            'auth': parsed.auth_type, 'endpoints': endpoints, 'notes': list(dict.fromkeys(notes))}
