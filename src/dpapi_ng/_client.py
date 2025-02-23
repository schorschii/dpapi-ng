# Copyright: (c) 2023, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

from __future__ import annotations

import time
import typing as t
import uuid

from ._asn1 import ASN1Writer
from ._blob import DPAPINGBlob
from ._crypto import (
    AlgorithmOID,
    cek_decrypt,
    cek_encrypt,
    cek_generate,
    content_decrypt,
    content_encrypt,
)
from ._dns import async_lookup_dc, lookup_dc
from ._epm import EPM, EptMap, EptMapResult, TCPFloor, build_tcpip_tower
from ._gkdi import (
    ISD_KEY,
    FFCDHParameters,
    GetKey,
    GroupKeyEnvelope,
    KDFParameters,
    compute_l1_key,
    compute_l2_key,
)
from ._rpc import (
    NDR,
    NDR64,
    BindAck,
    CommandFlags,
    CommandPContext,
    ContextElement,
    ContextResultCode,
    Response,
    VerificationTrailer,
    async_create_rpc_connection,
    bind_time_feature_negotiation,
    create_rpc_connection,
)
from ._security_descriptor import ace_to_bytes, sd_to_bytes

_EPOCH_FILETIME = 116444736000000000  # 1970-01-01 as FILETIME

_EPM_CONTEXTS = [
    ContextElement(
        context_id=0,
        abstract_syntax=EPM,
        transfer_syntaxes=[NDR64],
    )
]

_ISD_KEY_CONTEXTS = [
    ContextElement(
        context_id=0,
        abstract_syntax=ISD_KEY,
        transfer_syntaxes=[NDR64],
    ),
    ContextElement(
        context_id=1,
        abstract_syntax=ISD_KEY,
        transfer_syntaxes=[bind_time_feature_negotiation()],
    ),
]

_EPT_MAP_ISD_KEY = EptMap(
    obj=None,
    tower=build_tcpip_tower(
        service=ISD_KEY,
        data_rep=NDR,
        port=135,
        addr=0,
    ),
    entry_handle=None,
    max_towers=4,
)

_VERIFICATION_TRAILER = VerificationTrailer(
    [
        CommandPContext(
            flags=CommandFlags.SEC_VT_COMMAND_END,
            interface_id=ISD_KEY,
            transfer_syntax=NDR64,
        ),
    ]
)


def _process_bind_result(
    requested_contexts: t.List[ContextElement],
    bind_ack: BindAck,
    desired_context: int,
) -> None:
    accepted_ids = []
    for idx, c in enumerate(bind_ack.results):
        if c.result == ContextResultCode.ACCEPTANCE:
            ctx = requested_contexts[idx]
            accepted_ids.append(ctx.context_id)

    if desired_context not in accepted_ids:
        raise ValueError("Failed to bind to desired context")

    return


def _process_ept_map_result(
    response: Response,
) -> int:
    map_response = EptMapResult.unpack(response.stub_data)
    if map_response.status != 0:
        raise ValueError(f"Receive error during ept_map call 0x{map_response.status:08X}")

    for tower in map_response.towers:
        for floor in tower:
            if isinstance(floor, TCPFloor):
                return floor.port

    raise ValueError("Did not find expected TCP Port in ept_map response")


def _process_get_key_result(
    response: Response,
) -> GroupKeyEnvelope:
    pad_length = len(response.stub_data)
    if response.sec_trailer and response.sec_trailer.pad_length:
        pad_length -= response.sec_trailer.pad_length
    raw_resp = response.stub_data[:pad_length]
    return GetKey.unpack_response(raw_resp)


async def _async_get_key(
    server: str,
    target_sd: bytes,
    root_key_id: t.Optional[uuid.UUID],
    l0: int = -1,
    l1: int = -1,
    l2: int = -1,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
) -> GroupKeyEnvelope:
    rpc = await async_create_rpc_connection(server)
    async with rpc:
        context_id = _EPM_CONTEXTS[0].context_id
        ack = await rpc.bind(contexts=_EPM_CONTEXTS)
        _process_bind_result(_EPM_CONTEXTS, ack, context_id)

        ept_map = _EPT_MAP_ISD_KEY
        resp = await rpc.request(context_id, ept_map.opnum, ept_map.pack())
        isd_key_port = _process_ept_map_result(resp)

    rpc = await async_create_rpc_connection(
        server,
        isd_key_port,
        username=username,
        password=password,
        auth_protocol=auth_protocol,
    )
    async with rpc:
        context_id = _ISD_KEY_CONTEXTS[0].context_id
        ack = await rpc.bind(contexts=_ISD_KEY_CONTEXTS)
        _process_bind_result(_ISD_KEY_CONTEXTS, ack, context_id)

        get_key = GetKey(target_sd, root_key_id, l0, l1, l2)
        resp = await rpc.request(
            context_id,
            get_key.opnum,
            get_key.pack(),
            verification_trailer=_VERIFICATION_TRAILER,
        )
        return _process_get_key_result(resp)


def _sync_get_key(
    server: str,
    target_sd: bytes,
    root_key_id: t.Optional[uuid.UUID] = None,
    l0: int = -1,
    l1: int = -1,
    l2: int = -1,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
) -> GroupKeyEnvelope:
    with create_rpc_connection(server) as rpc:
        context_id = _EPM_CONTEXTS[0].context_id
        ack = rpc.bind(contexts=_EPM_CONTEXTS)
        _process_bind_result(_EPM_CONTEXTS, ack, context_id)

        ept_map = _EPT_MAP_ISD_KEY
        resp = rpc.request(0, ept_map.opnum, ept_map.pack())
        isd_key_port = _process_ept_map_result(resp)

    with create_rpc_connection(
        server,
        isd_key_port,
        username=username,
        password=password,
        auth_protocol=auth_protocol,
    ) as rpc:
        context_id = _ISD_KEY_CONTEXTS[0].context_id
        ack = rpc.bind(contexts=_ISD_KEY_CONTEXTS)
        _process_bind_result(_ISD_KEY_CONTEXTS, ack, context_id)

        get_key = GetKey(target_sd, root_key_id, l0, l1, l2)
        resp = rpc.request(
            context_id,
            get_key.opnum,
            get_key.pack(),
            verification_trailer=_VERIFICATION_TRAILER,
        )
        return _process_get_key_result(resp)


def _decrypt_blob(
    blob: DPAPINGBlob,
    key: GroupKeyEnvelope,
) -> bytes:
    kek = key.get_kek(blob.key_identifier)

    # With the kek we can unwrap the encrypted cek in the LAPS payload.
    cek = cek_decrypt(
        blob.enc_cek_algorithm,
        blob.enc_cek_parameters,
        kek,
        blob.enc_cek,
    )

    # With the cek we can decrypt the encrypted content in the LAPS payload.
    return content_decrypt(
        blob.enc_content_algorithm,
        blob.enc_content_parameters,
        cek,
        blob.enc_content,
    )


def _encrypt_blob(
    blob: bytes,
    key: GroupKeyEnvelope,
    security_descriptor: bytes,
    protection_descriptor: str,
) -> bytes:
    # Generate cek and encrypt our payload.
    enc_cek_algorithm = AlgorithmOID.AES256_WRAP
    cek, cek_iv = cek_generate(enc_cek_algorithm)

    parameters_writer = ASN1Writer()
    with parameters_writer.push_sequence() as parameters:
        parameters.write_octet_string(cek_iv)
        parameters.write_integer(16)

    enc_content_algorithm = AlgorithmOID.AES256_GCM
    enc_content_parameters = parameters_writer.get_data()
    enc_content = content_encrypt(
        enc_content_algorithm,
        enc_content_parameters,
        cek,
        blob,
    )

    kek, key_identifier = key.new_kek()
    enc_cek_parameters = None
    enc_cek = cek_encrypt(
        enc_cek_algorithm,
        enc_cek_parameters,
        kek,
        cek,
    )

    return DPAPINGBlob(
        key_identifier=key_identifier,
        security_descriptor=security_descriptor,
        enc_cek=enc_cek,
        enc_cek_algorithm=enc_cek_algorithm,
        enc_cek_parameters=enc_cek_parameters,
        enc_content=enc_content,
        enc_content_algorithm=enc_content_algorithm,
        enc_content_parameters=enc_content_parameters,
    ).pack(protection_descriptor)


def _get_protection_gke_from_cache(
    root_key_identifier: t.Optional[uuid.UUID],
    target_sd: bytes,
    cache: KeyCache,
) -> t.Optional[GroupKeyEnvelope]:
    if not root_key_identifier:
        return None

    # MS-GKDI 3.1.4.1 GetKey rules on how to generate the group key identifier
    # values from the current time
    # https://learn.microsoft.com/en-us/openspecs/windows_protocols/ms-gkdi/4cac87a3-521e-4918-a272-240f8fabed39
    current_time = (time.time_ns() // 100) + _EPOCH_FILETIME
    base = 360000000000  # 3.6 * 10**11
    l0 = int(current_time / (32 * 32 * base))
    l1 = int((current_time % (32 * 32 * base)) / (32 * base))
    l2 = int((current_time % (32 * base)) / base)

    rk = cache._get_key(
        target_sd,
        root_key_identifier,
        l0,
        l1,
        l2,
    )
    if not rk:
        return None

    kdf_parameters = KDFParameters.unpack(rk.kdf_parameters)
    l2_key = compute_l2_key(
        kdf_parameters.hash_algorithm,
        l1,
        l2,
        rk,
    )

    return GroupKeyEnvelope(
        version=rk.version,
        flags=rk.flags,
        l0=l0,
        l1=l1,
        l2=l2,
        root_key_identifier=root_key_identifier,
        kdf_algorithm=rk.kdf_algorithm,
        kdf_parameters=rk.kdf_parameters,
        secret_algorithm=rk.secret_algorithm,
        secret_parameters=rk.secret_parameters,
        private_key_length=rk.private_key_length,
        public_key_length=rk.public_key_length,
        domain_name=rk.domain_name,
        forest_name=rk.forest_name,
        l1_key=b"",
        l2_key=l2_key,
    )


class RootKey(t.NamedTuple):
    """The KDS Root Key."""

    key: bytes
    version: int
    kdf_algorithm: str
    kdf_parameters: bytes
    secret_algorithm: str
    secret_parameters: t.Optional[bytes]
    private_key_length: int
    public_key_length: int


class KeyCache:
    """Key Cache.

    This is a cache used to store the KDS keys. It can be used with
    :meth:`async_ncrypt_unprotect_secret` and :meth:`ncrypt_unprotect_secret`
    to avoid any extra RPC calls if the data was already retrieved.
    """

    def __init__(self) -> None:
        self._root_keys: t.Dict[uuid.UUID, RootKey] = {}
        self._seed_keys: t.Dict[uuid.UUID, t.Dict[bytes, t.Dict[int, GroupKeyEnvelope]]] = {}

    def load_key(
        self,
        key: bytes,
        root_key_id: uuid.UUID,
        version: int = 1,
        kdf_algorithm: str = "SP800_108_CTR_HMAC",
        kdf_parameters: t.Optional[bytes] = None,
        secret_algorithm: str = "DH",
        secret_parameters: t.Optional[bytes] = None,
        private_key_length: int = 512,
        public_key_length: int = 2048,
    ) -> None:
        """Load a KDS root key into the cache.

        This loads the KDS root key provided into the cache for use in future
        operations.

        A domain administrator can retrieve the required information from a DC
        using this PowerShell code.

        .. code-block:: powershell

            $configurationContext = (Get-ADRootDSE).configurationNamingContext
            $getParams = @{
                LDAPFilter = '(objectClass=msKds-ProvRootKey)'
                SearchBase = "CN=Master Root Keys,CN=Group Key Distribution Service,CN=Services,$configurationContext"
                SearchScope = 'OneLevel'
                Properties = @(
                    'cn'
                    'msKds-KDFAlgorithmID'
                    'msKds-KDFParam'
                    'msKds-SecretAgreementAlgorithmID'
                    'msKds-SecretAgreementParam'
                    'msKds-PrivateKeyLength'
                    'msKds-PublicKeyLength'
                    'msKds-RootKeyData'
                )
            }
            Get-ADObject @getParams | ForEach-Object {
                [PSCustomObject]@{
                    Version = 1
                    RootKeyId = [Guid]::new($_.cn)
                    KdfAlgorithm = $_.'msKds-KDFAlgorithmID'
                    KdfParameters = [System.Convert]::ToBase64String($_.'msKds-KDFParam')
                    SecretAgreementAlgorithm = $_.'msKds-SecretAgreementAlgorithmID'
                    SecretAgreementParameters = [System.Convert]::ToBase64String($_.'msKds-SecretAgreementParam')
                    PrivateKeyLength = $_.'msKds-PrivateKeyLength'
                    PublicKeyLength = $_.'msKds-PublicKeyLength'
                    RootKeyData = [System.Convert]::ToBase64String($_.'msKds-RootKeyData')
                }
            }

        It can also be retrieved using this OpenLDAP command:

        .. code-block:: bash

            ldapsearch \
                -b 'CN=Master Root Keys,CN=Group Key Distribution Service,CN=Services,CN=Configuration,DC=domain,DC=test' \
                -s one \
                '(objectClass=msKds-ProvRootKey)' \
                cn \
                msKds-KDFAlgorithmID \
                msKds-KDFParam \
                msKds-SecretAgreementAlgorithmID \
                msKds-SecretAgreementParam \
                msKds-PrivateKeyLength \
                msKds-PublicKeyLength \
                msKds-RootKeyData

        Args:
            key: The root key bytes stored in ``msKds-RootKeyData``.
            root_key_id: The root key id as stored in ``cn``.
            version: The key version number.
            kdf_algorithm: The KDF algorithm name stored in
                ``msKds-KDFAlgorithmID``.
            kdf_parameters: The KDF parameters stored in ``msKds-KDFParam`.
            secret_algorithm: The secret agreement algorithm stored in
                ``msKds-SecretAgreementAlgorithmID``.
            secret_parameters: The secret agreement parameters stored in
                ``msKds-SecretAgreementParam``.
            private_key_length: The private key length stored in
                ``msKds-PrivateKeyLength``.
            public_key_length: The public key length stored in
                ``msKds-PublicKeyLength``.
        """
        if not kdf_parameters:
            kdf_parameters = KDFParameters("SHA512").pack()

        if secret_algorithm == "DH" and not secret_parameters:
            # RFC 5114 - 2.3. 2048-bit MODP Group with 256-bit Prime Order Subgroup
            # https://www.rfc-editor.org/rfc/rfc5114#section-2.3
            secret_parameters = FFCDHParameters(
                key_length=256,
                field_order=17125458317614137930196041979257577826408832324037508573393292981642667139747621778802438775238728592968344613589379932348475613503476932163166973813218698343816463289144185362912602522540494983090531497232965829536524507269848825658311420299335922295709743267508322525966773950394919257576842038771632742044142471053509850123605883815857162666917775193496157372656195558305727009891276006514000409365877218171388319923896309377791762590614311849642961380224851940460421710449368927252974870395873936387909672274883295377481008150475878590270591798350563488168080923804611822387520198054002990623911454389104774092183,
                generator=8041367327046189302693984665026706374844608289874374425728797669509435881459140662650215832833471328470334064628508692231999401840332046192569287351991689963279656892562484773278584208040987631569628520464069532361274047374444344996651832979378318849943741662110395995778429270819222431610927356005913836932462099770076239554042855287138026806960470277326229482818003962004453764400995790974042663675692120758726145869061236443893509136147942414445551848162391468541444355707785697825741856849161233887307017428371823608125699892904960841221593344499088996021883972185241854777608212592397013510086894908468466292313,
            ).pack()

        self._root_keys[root_key_id] = RootKey(
            key=key,
            version=version,
            kdf_algorithm=kdf_algorithm,
            kdf_parameters=kdf_parameters,
            secret_algorithm=secret_algorithm,
            secret_parameters=secret_parameters,
            private_key_length=private_key_length,
            public_key_length=public_key_length,
        )

    def _get_key(
        self,
        target_sd: bytes,
        root_key_id: uuid.UUID,
        l0: int,
        l1: int,
        l2: int,
    ) -> t.Optional[GroupKeyEnvelope]:
        """Get key from the cache.

        Attempts to get the key from a cache if it's available. A key is cached
        either from the root key stored in :meth:`load_key` or from a previous
        RPC call for the same target sd and root key id.

        Args:
            target_sd: The target security descriptor the key is for.
            root_key_id: The root key id requested.
            l0: The L0 index needed.
            l1: The L1 index needed.
            l2: The L2 index needed.

        Returns:
            Optional[GroupKeyEnvelope]: The cached key if one was available.
        """
        seed_key = self._seed_keys.setdefault(root_key_id, {}).setdefault(target_sd, {}).get(l0, None)
        if seed_key and (seed_key.l1 > l1 or (seed_key.l1 == l1 and seed_key.l2 >= l2)):
            return seed_key

        root_key = self._root_keys.get(root_key_id, None)
        if root_key:
            l1_seed = compute_l1_key(
                target_sd,
                root_key_id,
                l0,
                root_key.key,
                KDFParameters.unpack(root_key.kdf_parameters).hash_algorithm,
            )

            gke = GroupKeyEnvelope(
                version=root_key.version,
                flags=2,
                l0=l0,
                l1=31,
                l2=31,
                root_key_identifier=root_key_id,
                kdf_algorithm=root_key.kdf_algorithm,
                kdf_parameters=root_key.kdf_parameters,
                secret_algorithm=root_key.secret_algorithm,
                secret_parameters=root_key.secret_parameters or b"",
                private_key_length=root_key.private_key_length,
                public_key_length=root_key.public_key_length,
                domain_name="",
                forest_name="",
                l1_key=l1_seed,
                l2_key=b"",
            )
            return self._seed_keys.setdefault(root_key_id, {}).setdefault(target_sd, {}).setdefault(l0, gke)

        return None

    def _store_key(
        self,
        target_sd: bytes,
        key: GroupKeyEnvelope,
    ) -> None:
        seed_key = self._seed_keys.setdefault(key.root_key_identifier, {}).setdefault(target_sd, {})

        existing = seed_key.get(key.l0, None)
        if not existing or key.l1 > existing.l1 or (key.l1 == existing.l1 and key.l2 > existing.l2):
            seed_key[key.l0] = key


def ncrypt_unprotect_secret(
    data: bytes,
    server: t.Optional[str] = None,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
    cache: t.Optional[KeyCache] = None,
) -> bytes:
    """Decrypt DPAPI-NG Blob.

    Decrypts the DPAPI-NG blob provided. This is meant to replicate the Win32
    API `NCryptUnprotectSecret`_.

    Decrypting the DPAPI-NG blob requires making an RPC call to the domain
    controller for the domain the blob was created in. It will attempt this
    by looking up the DC through an SRV lookup but ``server`` can be specified
    to avoid this SRV lookup.

    The RPC call requires the caller to authenticate before the key information
    is provided. This user must be one who is authorized to decrypt the secret.
    Explicit credentials can be specified, if none are the current Kerberos
    ticket retrieved by ``kinit`` will be used instead. Make sure to install
    the Kerberos extras package ``dpapi-ng[kerberos]`` to ensure Kerberos auth
    can be used.

    Args:
        data: The DPAPI-NG blob to decrypt.
        server: The domain controller to lookup the root key info.
        username: The username to decrypt the DPAPI-NG blob as.
        password: The password for the user.
        auth_protocol: The authentication protocol to use, defaults to
            ``negotiate`` but can be ``kerberos`` or ``ntlm``.
        cache: Optional cache that is used as the key source to avoid making
            the RPC call.

    Returns:
        bytes: The decrypt DPAPI-NG data.

    Raises:
        ValueError: An invalid data structure was found.
        NotImplementedError: An unknown value was found and has not been
            implemented yet.

    _NCryptUnprotectSecret:
        https://learn.microsoft.com/en-us/windows/win32/api/ncryptprotect/nf-ncryptprotect-ncryptunprotectsecret
    """
    blob = DPAPINGBlob.unpack(data)

    cache = cache or KeyCache()
    rk = cache._get_key(
        blob.security_descriptor,
        blob.key_identifier.root_key_identifier,
        blob.key_identifier.l0,
        blob.key_identifier.l1,
        blob.key_identifier.l2,
    )

    if not rk:
        if not server:
            srv = lookup_dc(blob.key_identifier.domain_name)
            server = srv.target

        rk = _sync_get_key(
            server,
            blob.security_descriptor,
            blob.key_identifier.root_key_identifier,
            blob.key_identifier.l0,
            blob.key_identifier.l1,
            blob.key_identifier.l2,
            username=username,
            password=password,
            auth_protocol=auth_protocol,
        )

    if not rk.is_public_key:
        cache._store_key(blob.security_descriptor, rk)

    return _decrypt_blob(blob, rk)


def ncrypt_protect_secret(
    data: bytes,
    protection_descriptor: str,
    root_key_identifier: t.Optional[uuid.UUID] = None,
    server: t.Optional[str] = None,
    domain_name: t.Optional[str] = None,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
    cache: t.Optional[KeyCache] = None,
) -> bytes:
    """Encrypt DPAPI-NG Blob.

    Encrypts the blob provided as DPAPI-NG Blob. This is meant to
    replicate the Win32 API `NCryptProtectSecret`_. While NCryptProtectSecret
    supports multiple protection descriptor values, currently only the SID type
    is supported.

    Encrypting the DPAPI-NG blob requires making an RPC call to the domain
    controller for the domain the blob was created in. It will attempt this
    by looking up the DC through an SRV lookup but ``server`` can be specified
    to avoid this SRV lookup.

    The RPC call requires the caller to authenticate before the key information
    is provided. Explicit credentials can be specified, if none are then the
    current Kerberos ticket retrieved by ``kinit`` will be used instead. Make
    sure to install the Kerberos extras package ``dpapi-ng[kerberos]`` to ensure
    Kerberos auth can be used.

    Args:
        data: The bytes blob to encrypt.
        protection_descriptor: The security identifier to protect the secret
            with.
        root_key_identifier: Use the root key identified by this id, if not set,
            the root key id returned by the server will be used.
        server: The domain controller to lookup the root key info.
        domain_name: The domain name to query the domain controller hostname
            via DNS.
        username: The username to encrypt the DPAPI-NG blob as.
        password: The password for the user.
        auth_protocol: The authentication protocol to use, defaults to
            ``negotiate`` but can be ``kerberos`` or ``ntlm``.
        cache: Optional cache that is used as the key source to avoid making
            the RPC call. This only works if root_key_identifier is also
            specified.

    Returns:
        bytes: The encrypted DPAPI-NG data.

    Raises:
        ValueError: An invalid data structure was found.
        NotImplementedError: An unknown value was found and has not been
            implemented yet.

    _NCryptProtectSecret:
        https://learn.microsoft.com/en-us/windows/win32/api/ncryptprotect/nf-ncryptprotect-ncryptprotectsecret
    """
    l0 = -1
    l1 = -1
    l2 = -1

    sd = sd_to_bytes(
        owner="S-1-5-18",
        group="S-1-5-18",
        dacl=[ace_to_bytes(protection_descriptor, 3), ace_to_bytes("S-1-1-0", 2)],
    )

    cache = cache or KeyCache()
    rk = _get_protection_gke_from_cache(root_key_identifier, sd, cache)

    if not rk:
        if not server:
            srv = lookup_dc(domain_name)
            server = srv.target

        rk = _sync_get_key(
            server,
            sd,
            root_key_identifier,
            l0,
            l1,
            l2,
            username=username,
            password=password,
            auth_protocol=auth_protocol,
        )

    if not rk.is_public_key:
        cache._store_key(sd, rk)

    return _encrypt_blob(data, rk, sd, protection_descriptor)


async def async_ncrypt_unprotect_secret(
    data: bytes,
    server: t.Optional[str] = None,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
    cache: t.Optional[KeyCache] = None,
) -> bytes:
    """Decrypt DPAPI-NG Blob.

    Decrypts the DPAPI-NG blob provided. This is meant to replicate the Win32
    API `NCryptUnprotectSecret`_.

    Decrypting the DPAPI-NG blob requires making an RPC call to the domain
    controller for the domain the blob was created in. It will attempt this
    by looking up the DC through an SRV lookup but ``server`` can be specified
    to avoid this SRV lookup.

    The RPC call requires the caller to authenticate before the key information
    is provided. This user must be one who is authorized to decrypt the secret.
    Explicit credentials can be specified, if none are the current Kerberos
    ticket retrieved by ``kinit`` will be used instead. Make sure to install
    the Kerberos extras package ``dpapi-ng[kerberos]`` to ensure Kerberos auth
    can be used.

    Args:
        data: The DPAPI-NG blob to decrypt.
        server: The domain controller to lookup the root key info.
        username: The username to decrypt the DPAPI-NG blob as.
        password: The password for the user.
        auth_protocol: The authentication protocol to use, defaults to
            ``negotiate`` but can be ``kerberos`` or ``ntlm``.
        cache: Optional cache that is used as the key source to avoid making
            the RPC call.

    Returns:
        bytes: The decrypt DPAPI-NG data.

    Raises:
        ValueError: An invalid data structure was found.
        NotImplementedError: An unknown value was found and has not been
            implemented yet.

    _NCryptUnprotectSecret:
        https://learn.microsoft.com/en-us/windows/win32/api/ncryptprotect/nf-ncryptprotect-ncryptunprotectsecret
    """
    blob = DPAPINGBlob.unpack(data)

    cache = cache or KeyCache()
    rk = cache._get_key(
        blob.security_descriptor,
        blob.key_identifier.root_key_identifier,
        blob.key_identifier.l0,
        blob.key_identifier.l1,
        blob.key_identifier.l2,
    )
    if not rk:
        if not server:
            srv = await async_lookup_dc(blob.key_identifier.domain_name)
            server = srv.target

        rk = await _async_get_key(
            server,
            blob.security_descriptor,
            blob.key_identifier.root_key_identifier,
            blob.key_identifier.l0,
            blob.key_identifier.l1,
            blob.key_identifier.l2,
            username=username,
            password=password,
            auth_protocol=auth_protocol,
        )

    if not rk.is_public_key:
        cache._store_key(blob.security_descriptor, rk)

    return _decrypt_blob(blob, rk)


async def async_ncrypt_protect_secret(
    data: bytes,
    protection_descriptor: str,
    root_key_identifier: t.Optional[uuid.UUID] = None,
    server: t.Optional[str] = None,
    domain_name: t.Optional[str] = None,
    username: t.Optional[str] = None,
    password: t.Optional[str] = None,
    auth_protocol: str = "negotiate",
    cache: t.Optional[KeyCache] = None,
) -> bytes:
    """Encrypt DPAPI-NG Blob.

    Encrypts the blob provided as DPAPI-NG Blob. This is meant to
    replicate the Win32 API `NCryptProtectSecret`_. While NCryptProtectSecret
    supports multiple protection descriptor values, currently only the SID type
    is supported.

    Encrypting the DPAPI-NG blob requires making an RPC call to the domain
    controller for the domain the blob was created in. It will attempt this
    by looking up the DC through an SRV lookup but ``server`` can be specified
    to avoid this SRV lookup.

    The RPC call requires the caller to authenticate before the key information
    is provided. Explicit credentials can be specified, if none are then the
    current Kerberos ticket retrieved by ``kinit`` will be used instead. Make
    sure to install the Kerberos extras package ``dpapi-ng[kerberos]`` to ensure
    Kerberos auth can be used.

    Args:
        data: The bytes blob to encrypt.
        protection_descriptor: The security identifier to protect the secret
            with.
        root_key_identifier: Use the root key identified by this id, if not set,
            the root key id returned by the server will be used.
        server: The domain controller to lookup the root key info.
        domain_name: The domain name to query the domain controller hostname
            via DNS.
        username: The username to encrypt the DPAPI-NG blob as.
        password: The password for the user.
        auth_protocol: The authentication protocol to use, defaults to
            ``negotiate`` but can be ``kerberos`` or ``ntlm``.
        cache: Optional cache that is used as the key source to avoid making
            the RPC call. This only works if root_key_identifier is also
            specified.

    Returns:
        bytes: The encrypted DPAPI-NG data.

    Raises:
        ValueError: An invalid data structure was found.
        NotImplementedError: An unknown value was found and has not been
            implemented yet.

    _NCryptProtectSecret:
        https://learn.microsoft.com/en-us/windows/win32/api/ncryptprotect/nf-ncryptprotect-ncryptprotectsecret
    """
    l0 = -1
    l1 = -1
    l2 = -1

    sd = sd_to_bytes(
        owner="S-1-5-18",
        group="S-1-5-18",
        dacl=[ace_to_bytes(protection_descriptor, 3), ace_to_bytes("S-1-1-0", 2)],
    )

    cache = cache or KeyCache()
    rk = _get_protection_gke_from_cache(root_key_identifier, sd, cache)

    if not rk:
        if not server:
            srv = await async_lookup_dc(domain_name)
            server = srv.target

        rk = await _async_get_key(
            server,
            sd,
            root_key_identifier,
            l0,
            l1,
            l2,
            username=username,
            password=password,
            auth_protocol=auth_protocol,
        )

    if not rk.is_public_key:
        cache._store_key(sd, rk)

    return _encrypt_blob(data, rk, sd, protection_descriptor)
