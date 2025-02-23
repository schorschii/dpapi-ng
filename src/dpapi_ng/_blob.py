# Copyright: (c) 2023, Jordan Borean (@jborean93) <jborean93@gmail.com>
# MIT License (see LICENSE or https://opensource.org/licenses/MIT)

from __future__ import annotations

import dataclasses
import typing as t
import uuid

from ._asn1 import ASN1Reader, ASN1Tag, ASN1Writer, TagClass, TypeTagNumber
from ._pkcs7 import (
    ContentInfo,
    EnvelopedData,
    KEKRecipientInfo,
    NCryptProtectionDescriptor,
)
from ._security_descriptor import ace_to_bytes, sd_to_bytes


@dataclasses.dataclass(frozen=True)
class KeyIdentifier:
    """Key Identifier.

    This contains the key identifier info that can be used by MS-GKDI GetKey
    to retrieve the group key seed values. This structure is not defined
    publicly by Microsoft but it closely matches the :class:`GroupKeyEnvelope`
    structure.

    Args:
        version: The version of the structure, should be 1
        flags: Flags describing the values inside the structure
        l0: The L0 index of the key
        l1: The L1 index of the key
        l2: The L2 index of the key
        root_key_identifier: The key identifier
        key_info: If is_public_key this is the public key, else it is the key
            KDF context value.
        domain_name: The domain name of the server in DNS format.
        forest_name: The forest name of the server in DNS format.
    """

    version: int
    magic: bytes = dataclasses.field(init=False, repr=False, default=b"\x4B\x44\x53\x4B")
    flags: int
    l0: int
    l1: int
    l2: int
    root_key_identifier: uuid.UUID
    key_info: bytes
    domain_name: str
    forest_name: str

    @property
    def is_public_key(self) -> bool:
        return bool(self.flags & 1)

    def pack(self) -> bytes:
        b_domain_name = (self.domain_name + "\00").encode("utf-16-le")
        b_forest_name = (self.forest_name + "\00").encode("utf-16-le")

        return b"".join(
            [
                self.version.to_bytes(4, byteorder="little"),
                self.magic,
                self.flags.to_bytes(4, byteorder="little"),
                self.l0.to_bytes(4, byteorder="little"),
                self.l1.to_bytes(4, byteorder="little"),
                self.l2.to_bytes(4, byteorder="little"),
                self.root_key_identifier.bytes_le,
                len(self.key_info).to_bytes(4, byteorder="little"),
                len(b_domain_name).to_bytes(4, byteorder="little"),
                len(b_forest_name).to_bytes(4, byteorder="little"),
                self.key_info,
                b_domain_name,
                b_forest_name,
            ]
        )

    @classmethod
    def unpack(
        cls,
        data: t.Union[bytes, bytearray, memoryview],
    ) -> KeyIdentifier:
        view = memoryview(data)

        version = int.from_bytes(view[:4], byteorder="little")

        if view[4:8].tobytes() != cls.magic:
            raise ValueError(f"Failed to unpack {cls.__name__} as magic identifier is invalid")

        flags = int.from_bytes(view[8:12], byteorder="little")
        l0_index = int.from_bytes(view[12:16], byteorder="little")
        l1_index = int.from_bytes(view[16:20], byteorder="little")
        l2_index = int.from_bytes(view[20:24], byteorder="little")
        root_key_identifier = uuid.UUID(bytes_le=view[24:40].tobytes())
        key_info_len = int.from_bytes(view[40:44], byteorder="little")
        domain_len = int.from_bytes(view[44:48], byteorder="little")
        forest_len = int.from_bytes(view[48:52], byteorder="little")
        view = view[52:]

        key_info = view[:key_info_len].tobytes()
        view = view[key_info_len:]

        # Take away 2 for the final null padding
        domain = view[: domain_len - 2].tobytes().decode("utf-16-le")
        view = view[domain_len:]

        forest = view[: forest_len - 2].tobytes().decode("utf-16-le")
        view = view[forest_len:]

        return KeyIdentifier(
            version=version,
            flags=flags,
            l0=l0_index,
            l1=l1_index,
            l2=l2_index,
            root_key_identifier=root_key_identifier,
            key_info=key_info,
            domain_name=domain,
            forest_name=forest,
        )


@dataclasses.dataclass
class DPAPINGBlob:
    MICROSOFT_SOFTWARE_OID = "1.3.6.1.4.1.311.74.1"
    MICROSOFT_SOFTWARE_SYSTEMS_OID = "1.3.6.1.4.1.311.74.1.1"

    """DPAPI NG Blob.

    The unpacked DPAPI NG blob that contains the information needed to decrypt
    the encrypted content. The key identifier and protection descriptor can be
    used to generate the KEK. The KEK is used to decrypt the encrypted CEK. The
    CEK can be used to decrypt the encrypted contents.

    Args:
        key_identifier: The key identifier for the KEK.
        security_descriptor: The Security Descriptor that protects the key.
        enc_cek: The encrypted CEK.
        enc_cek_algorithm: The encrypted CEK algorithm OID.
        enc_cek_parameters: The encrypted CEK algorithm parameters.
        enc_content: The encrypted content.
        enc_content_algorithm: The encrypted content algorithm OID.
        enc_content_parameters: The encrypted content parameters.
    """

    key_identifier: KeyIdentifier
    security_descriptor: bytes
    enc_cek: bytes
    enc_cek_algorithm: str
    enc_cek_parameters: t.Optional[bytes]
    enc_content: bytes
    enc_content_algorithm: str
    enc_content_parameters: t.Optional[bytes]

    def pack(
        self,
        protection_descriptor: str,
        blob_in_envelope: bool = True,
    ) -> bytes:
        """
        Args:
            protection_descriptor: The protection descriptor to embed in the EnvelopedData structure.
            blob_in_envelope: True to store the encrypted blob in the EnvelopedData structure (NCryptProtectSecret general),
                False to append the encrypted blob after the EnvelopedData structure (LAPS style).

        Returns:
            bytes: The DPAPI NG Blob data.
        """
        # TODO: it's not very nice to pass protection_descriptor as separate parameter here, should be extracted from self.security_descriptor
        writer = ASN1Writer()
        with writer.push_sequence() as ContentInfo:
            ContentInfo.write_object_identifier(EnvelopedData.CONTENT_TYPE_ENVELOPED_DATA_OID)
            with ContentInfo.push_sequence(
                ASN1Tag(tag_class=TagClass.CONTEXT_SPECIFIC, tag_number=0, is_constructed=True)
            ) as Content:
                with Content.push_sequence() as enveloped_data:
                    enveloped_data.write_integer(2)  # EnvelopedData CMSVersion
                    with enveloped_data.push_set() as recipient_infos:
                        with recipient_infos.push_sequence(
                            ASN1Tag(tag_class=TagClass.CONTEXT_SPECIFIC, tag_number=2, is_constructed=True)
                        ) as recipient_info:
                            recipient_info.write_integer(4)  # KEKRecipientInfo CMSVersion
                            with recipient_info.push_sequence() as key_agree_recipient_info:
                                key_agree_recipient_info.write_octet_string(self.key_identifier.pack())
                                with key_agree_recipient_info.push_sequence() as originator:
                                    originator.write_object_identifier(DPAPINGBlob.MICROSOFT_SOFTWARE_OID)
                                    with originator.push_sequence() as originator_sequence:
                                        originator_sequence.write_object_identifier(
                                            DPAPINGBlob.MICROSOFT_SOFTWARE_SYSTEMS_OID
                                        )
                                        with originator_sequence.push_sequence() as originator_sequence_2:
                                            with originator_sequence_2.push_sequence() as originator_sequence_3:
                                                with originator_sequence_3.push_sequence() as originator_sequence_4:
                                                    originator_sequence_4.write_octet_string(
                                                        b"SID", ASN1Tag.universal_tag(TypeTagNumber.UTF8_STRING)
                                                    )
                                                    originator_sequence_4.write_octet_string(
                                                        protection_descriptor.encode("utf-8"),
                                                        ASN1Tag.universal_tag(TypeTagNumber.UTF8_STRING),
                                                    )
                            with recipient_info.push_sequence() as kek_recipient_info:
                                kek_recipient_info.write_object_identifier(self.enc_cek_algorithm)
                            recipient_info.write_octet_string(self.enc_cek)
                    with enveloped_data.push_sequence() as encrypted_content_info:
                        encrypted_content_info.write_object_identifier(EnvelopedData.CONTENT_TYPE_DATA_OID)
                        with encrypted_content_info.push_sequence() as content_encryption_algorithm_identifier:
                            content_encryption_algorithm_identifier.write_object_identifier(self.enc_content_algorithm)
                            if self.enc_content_parameters:
                                content_encryption_algorithm_identifier._data.extend(self.enc_content_parameters)
                            else:
                                content_encryption_algorithm_identifier._data.extend(b"")
                        if blob_in_envelope:
                            encrypted_content_info.write_octet_string(
                                self.enc_content,
                                tag=ASN1Tag(tag_class=TagClass.CONTEXT_SPECIFIC, tag_number=0, is_constructed=False),
                            )

        return b"".join(
            [
                writer.get_data(),
                self.enc_content if not blob_in_envelope else b"",
            ]
        )

    @classmethod
    def unpack(
        cls,
        data: t.Union[bytes, bytearray, memoryview],
    ) -> DPAPINGBlob:
        view = memoryview(data)
        header = ASN1Reader(view).peek_header()
        content_info = ContentInfo.unpack(view[: header.tag_length + header.length], header=header)
        remaining_data = view[header.tag_length + header.length :]

        if content_info.content_type != EnvelopedData.CONTENT_TYPE_ENVELOPED_DATA_OID:
            raise ValueError(f"DPAPI-NG blob content type '{content_info.content_type}' is unsupported")
        enveloped_data = EnvelopedData.unpack(content_info.content)

        if (
            enveloped_data.version != 2
            or len(enveloped_data.recipient_infos) != 1
            or not isinstance(enveloped_data.recipient_infos[0], KEKRecipientInfo)
            or enveloped_data.recipient_infos[0].version != 4
        ):
            raise ValueError(f"DPAPI-NG blob is not in the expected format")

        kek_info = enveloped_data.recipient_infos[0]
        key_identifier = KeyIdentifier.unpack(kek_info.kekid.key_identifier)

        if not kek_info.kekid.other or kek_info.kekid.other.key_attr_id != DPAPINGBlob.MICROSOFT_SOFTWARE_OID:
            raise ValueError("DPAPI-NG KEK Id is not in the expected format")

        protection_descriptor = NCryptProtectionDescriptor.unpack(kek_info.kekid.other.key_attr or b"")
        if (
            protection_descriptor.content_type != DPAPINGBlob.MICROSOFT_SOFTWARE_SYSTEMS_OID
            or protection_descriptor.type != "SID"
        ):
            raise ValueError(f"DPAPI-NG protection descriptor type '{protection_descriptor.type}' is unsupported")

        # Build the target security descriptor from the SID passed in. This SD
        # contains an ACE per target user with a mask of 0x3 and a final ACE of
        # the current user with a mask of 0x2. When viewing this over the wire
        # the current user is set as S-1-1-0 (World) and the owner/group is
        # S-1-5-18 (SYSTEM).
        target_sd = sd_to_bytes(
            owner="S-1-5-18",
            group="S-1-5-18",
            dacl=[ace_to_bytes(protection_descriptor.value, 3), ace_to_bytes("S-1-1-0", 2)],
        )

        # Some DPAPI blobs don't include the content in the PKCS7 payload but
        # just append after the blob.
        enc_content = enveloped_data.encrypted_content_info.content or remaining_data.tobytes()

        return DPAPINGBlob(
            key_identifier=key_identifier,
            security_descriptor=target_sd,
            enc_cek=kek_info.encrypted_key,
            enc_cek_algorithm=kek_info.key_encryption_algorithm.algorithm,
            enc_cek_parameters=kek_info.key_encryption_algorithm.parameters,
            enc_content=enc_content,
            enc_content_algorithm=enveloped_data.encrypted_content_info.algorithm.algorithm,
            enc_content_parameters=enveloped_data.encrypted_content_info.algorithm.parameters,
        )
